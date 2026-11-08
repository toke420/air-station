# SAFE FALLBACK: the v2.0 audio config (instant Mixxx/OBS takeover), known to work
# on this VPS. Only used if config/radio.liq fails 'airctl check-config':
#   cp config/radio-safe.liq config/radio.liq && docker compose up -d liquidsoap

# AirStation programme (Liquidsoap 2.4.x, official savonet/liquidsoap image)
#
# Priority, highest first:
#   1. Mixxx live DJ      harbor mount /live on port 8005
#   2. OBS via MediaMTX   harbor mount /obs  (MediaMTX pushes the audio here)
#   3. AutoDJ             next track chosen by the AirStation API from the local cache
#   4. Emergency folder   data/emergency, used when the API has nothing to give
#   5. Silence            last resort so listeners stay connected
#
# Live takes over immediately (track_sensitive=false). A live source that stays
# silent for 15 s is dropped, so a forgotten open Mixxx session doesn't leave
# dead air. When live ends, AutoDJ starts a fresh track instead of resuming the
# one it was interrupted in.
#
# This file never reads the NAS. Validate after any edit with:
#   docker compose run --rm --no-deps liquidsoap --check /app/config/radio.liq

settings.log.stdout := true
settings.log.file := false
settings.log.level := 3
settings.server.telnet := true
settings.server.telnet.bind_addr := "0.0.0.0"
settings.server.telnet.port := 1234

def read_secret(name) =
  string.trim(file.contents("/run/secrets/#{name}"))
end

source_password = read_secret("source_password")
dj_password = read_secret("dj_password")
obs_password = read_secret("obs_harbor_password")
api_token = read_secret("internal_token")
api_url = environment.get(default="http://api:8080", "AIR_API_URL")
station_name = environment.get(default="AirStation", "AIR_STATION_NAME")
station_url = environment.get(default="https://radio.example.com", "AIR_STATION_URL")

# ------------------------------------------------------------------ AutoDJ
def next_uri() =
  try
    reply = http.get(timeout=8., headers=[("X-Air-Token", api_token)],
                     "#{api_url}/internal/next")
    if reply.status_code == 200 then string.trim(reply) else "" end
  catch err do
    ignore(err)
    log.important("AutoDJ: API not reachable; emergency folder will play")
    ""
  end
end

def next_request() =
  uri = next_uri()
  if uri == "" then
    null
  else
    request.create(uri)
  end
end

autodj_api = request.dynamic(id="autodj_api", retry_delay=5., next_request)

emergency = playlist(id="emergency", mode="randomize", reload=300,
                     reload_mode="seconds", "/emergency")

autodj = fallback(id="autodj", track_sensitive=true, [autodj_api, emergency])

# -------------------------------------------------------------------- live
live_raw = input.harbor(id="live_in", port=8005, user="source",
                        password=dj_password, buffer=5., max=20., "live")
obs_raw = input.harbor(id="obs_in", port=8005, user="source",
                       password=obs_password, buffer=5., max=20., "obs")

live_audible = blank.strip(id="live_strip", max_blank=15., threshold=-45., live_raw)
obs_audible = blank.strip(id="obs_strip", max_blank=15., threshold=-45., obs_raw)

# The admin page can block live takeover (e.g. to kick a DJ back to AutoDJ).
live_allowed = ref(environment.get(default="on", "AIR_LIVE_ENABLED") != "off")
live_gated = switch(id="live_gate", track_sensitive=false,
                    [({live_allowed()}, live_audible)])
obs_gated = switch(id="obs_gate", track_sensitive=false,
                   [({live_allowed()}, obs_audible)])

programme = fallback(id="programme", track_sensitive=false,
                     [live_gated, obs_gated, autodj])
radio = mksafe(programme)

# When a live source takes over, skip the interrupted AutoDJ track so the
# rotation comes back with a fresh song afterwards.
was_live = ref(false)
def watch_live() =
  now_live = live_gated.is_ready() or obs_gated.is_ready()
  if now_live and not was_live() then
    log.important("Live source on air")
    autodj.skip()
  end
  if was_live() and not now_live then
    log.important("Live source ended; back to AutoDJ")
  end
  was_live := now_live
end
thread.run(every=1., watch_live)

# ---------------------------------------------------------------- recording
# off  = nothing; live = only Mixxx/OBS sets; all = the whole programme.
# Files: data/recordings/YYYY-MM-DD/air-YYYYmmdd-HHMMSS.mp3 (a new file each
# time a live set starts). The API switches this off below the disk reserve.
record_mode = ref(environment.get(default="off", "AIR_RECORD_MODE"))
rec_source = switch(id="rec_switch", track_sensitive=false, [
  ({record_mode() == "live"}, live_raw),
  ({record_mode() == "live"}, obs_raw),
  ({record_mode() == "all"}, radio)
])
output.file(id="recorder", fallible=true,
            %mp3(bitrate=192, samplerate=44100, stereo=true),
            "/recordings/%Y-%m-%d/air-%Y%m%d-%H%M%S.mp3", rec_source)

# ------------------------------------------------------------------ outputs
output.icecast(%mp3(bitrate=192, samplerate=44100, stereo=true),
  id="out_hq", host="icecast", port=8000, password=source_password,
  mount="radio.mp3", name=station_name, url=station_url,
  description="Real music. Real people. No borders.",
  genre="Hip Hop, Rap, Trap, Grime, R&B", public=false, radio)

output.icecast(%mp3(bitrate=96, samplerate=44100, stereo=true),
  id="out_lq", host="icecast", port=8000, password=source_password,
  mount="radio-low.mp3", name="#{station_name} (data saver)", url=station_url,
  description="Real music. Real people. No borders.",
  genre="Hip Hop, Rap, Trap, Grime, R&B", public=false, radio)

# ------------------------------------------------------- control (telnet)
# Used by the API container only; port 1234 is not published on the host.
def air_state(_) =
  src =
    if live_gated.is_ready() then "live"
    elsif obs_gated.is_ready() then "obs"
    else "autodj" end
  m =
    if src == "live" then null.get(default=[], live_raw.last_metadata())
    elsif src == "obs" then null.get(default=[], obs_raw.last_metadata())
    else null.get(default=[], radio.last_metadata()) end
  json.stringify({
    source = src,
    title = m["title"],
    artist = m["artist"],
    album = m["album"],
    air_id = m["air_id"],
    live_connected = live_raw.is_ready(),
    obs_connected = obs_raw.is_ready(),
    live_enabled = live_allowed(),
    record = record_mode()
  })
end

def air_skip(_) =
  autodj.skip()
  "Skipped"
end

def air_live(arg) =
  live_allowed := (string.trim(arg) != "off")
  if live_allowed() then "live takeover on" else "live takeover off" end
end

def air_record(arg) =
  mode = string.trim(arg)
  if mode == "off" or mode == "live" or mode == "all" then
    record_mode := mode
    "record mode #{mode}"
  else
    "usage: air.record off|live|all"
  end
end

server.register(namespace="air", description="Station state as JSON", "state", air_state)
server.register(namespace="air", description="Skip the current AutoDJ track", "skip", air_skip)
server.register(namespace="air", usage="live on|off", description="Allow live takeover", "live", air_live)
server.register(namespace="air", usage="record off|live|all", description="Recording mode", "record", air_record)
