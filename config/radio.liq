# AirStation programme (Liquidsoap 2.4.x, official savonet/liquidsoap image)
#
# Priority, highest first:
#   1. Mixxx live DJ      harbor mount /live on port 8005
#   2. OBS via MediaMTX   harbor mount /obs  (MediaMTX pushes the audio here)
#   3. AutoDJ             next track chosen by the AirStation API from the local cache
#   4. Emergency folder   data/emergency, used when the API has nothing to give
#   5. Silence            last resort so listeners stay connected
#
# Studio desk (admin dashboard, see "studio desk" below):
#   - the browser microphone arrives on harbor mount /mic (WebSocket, through
#     Caddy) and is mixed on top of whatever is on air (voice-over);
#   - air.volume sets the level of the music under the voice;
#   - air.pause stops AutoDJ when the current song ends (talk break) until
#     it is switched off again.
#
# Live takeover timing (AIR_LIVE_SWITCH in .env):
#   track      (default) a DJ who connects goes on air when the current AutoDJ
#              song ends. "Go live now" in the admin cuts in straight away.
#   immediate  cut straight to the DJ; the interrupted song is skipped so
#              AutoDJ comes back with a fresh one.
# A live source that stays silent for 15 s is dropped, so a forgotten open
# Mixxx session doesn't leave dead air. Ending a live set always hands back
# to AutoDJ straight away.
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
                        password=dj_password, buffer=5., max=10., "live")
obs_raw = input.harbor(id="obs_in", port=8005, user="source",
                       password=obs_password, buffer=5., max=10., "obs")

live_audible = blank.strip(id="live_strip", max_blank=15., threshold=-45., live_raw)
obs_audible = blank.strip(id="obs_strip", max_blank=15., threshold=-45., obs_raw)

# The admin page can block live takeover (e.g. to kick a DJ back to AutoDJ).
live_allowed = ref(environment.get(default="on", "AIR_LIVE_ENABLED") != "off")

live_switch = environment.get(default="track", "AIR_LIVE_SWITCH")
wait_for_song = live_switch != "immediate"

# live_open lets a connected DJ on air. "immediate": as soon as Mixxx/OBS
# connects. "track": when the current AutoDJ song ends (watch_live below), or
# straight away with "Go live now" in the admin (air.skip).
# The programme itself always switches instantly (track_sensitive=false), the
# same simple setup as v2.0; the waiting is done by opening this gate.
live_open = ref(false)

# ------------------------------------------------------------ studio desk
# Talk break: with pause_wanted set, AutoDJ finishes the current song and then
# stays quiet (the switch only looks at its condition between songs).
pause_wanted = ref(false)
music = switch(id="music", track_sensitive=true, [({not pause_wanted()}, autodj)])

def music_stopped() =
  pause_wanted() and not music.is_ready()
end

# Music level under the microphone: 0.0 to 1.0, moved in small steps so a
# change in the admin is a short fade instead of a jump.
music_target = ref(1.)
music_gain = ref(1.)
def glide_music() =
  diff = music_target() - music_gain()
  if diff > 0.04 then
    music_gain := music_gain() + 0.04
  elsif diff < -0.04 then
    music_gain := music_gain() - 0.04
  else
    music_gain := music_target()
  end
end
thread.run(every=0.05, glide_music)

# Browser microphone. No fixed password: the API hands the logged-in admin a
# one-time token and tells this script about it (air.mic_token); nothing
# connects before that.
mic_token = ref("")
def mic_auth(login) =
  mic_token() != "" and login.password == mic_token()
end
mic_raw = input.harbor(id="mic_in", port=8005, auth=mic_auth,
                       buffer=1., max=4., "mic")

live_gated = switch(id="live_gate", track_sensitive=false,
                    [({live_allowed() and live_open()}, live_audible)])
obs_gated = switch(id="obs_gate", track_sensitive=false,
                   [({live_allowed() and live_open()}, obs_audible)])

programme = fallback(id="programme", track_sensitive=false,
                     [live_gated, obs_gated, music])
# Everything on air at the chosen level, with the microphone on top at full level.
bed = amplify(id="music_level", {music_gain()}, mksafe(programme))
radio = add(id="air_mix", normalize=false, [bed, mic_raw])

def dj_connected() =
  live_raw.is_ready() or obs_raw.is_ready()
end

def song_key() =
  m = null.get(default=[], autodj.last_metadata())
  m["air_id"] ^ "|" ^ m["artist"] ^ "|" ^ m["title"]
end

waiting_from = ref("")
was_live = ref(false)
def watch_live() =
  if not dj_connected() then
    live_open := false
    waiting_from := ""
  elsif (not live_open()) and live_allowed() then
    if (not wait_for_song) or music_stopped() then
      live_open := true
    else
      song = song_key()
      if waiting_from() == "" then
        waiting_from := song
        log.important("DJ connected: going live when the current song ends")
      end
      left = autodj.remaining()
      if song != waiting_from() or (left >= 0. and left < 0.5) then
        live_open := true
      end
    end
  end
  now_live = live_gated.is_ready() or obs_gated.is_ready()
  if now_live and not was_live() then
    log.important("Live source on air")
    # Skip what AutoDJ was on, so it comes back with a fresh song afterwards.
    autodj.skip()
  end
  if was_live() and not now_live then
    log.important("Live source ended; back to AutoDJ")
  end
  was_live := now_live
end
thread.run(every=0.25, watch_live)

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
    else null.get(default=[], autodj.last_metadata()) end
  json.stringify({
    source = src,
    title = m["title"],
    artist = m["artist"],
    album = m["album"],
    air_id = m["air_id"],
    live_connected = live_raw.is_ready(),
    obs_connected = obs_raw.is_ready(),
    live_enabled = live_allowed(),
    live_waiting = dj_connected() and live_allowed() and src == "autodj",
    live_switch = live_switch,
    record = record_mode(),
    mic_connected = mic_raw.is_ready(),
    music_volume = int_of_float(music_target() * 100. + 0.5),
    pause = pause_wanted(),
    music_stopped = music_stopped() and src == "autodj"
  })
end

# "Skip track" in the admin; while a DJ is waiting it is "Go live now".
def air_skip(_) =
  if dj_connected() and live_allowed() and (not live_open()) then
    live_open := true
    "Going live"
  else
    autodj.skip()
    "Skipped"
  end
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

def air_volume(arg) =
  level = float_of_string(default=-1., string.trim(arg))
  if level < 0. or level > 100. then
    "usage: air.volume 0-100"
  else
    music_target := level / 100.
    "music volume #{int_of_float(level)}"
  end
end

def air_pause(arg) =
  pause_wanted := (string.trim(arg) == "on")
  if pause_wanted() then "music stops after this song" else "music on" end
end

def air_mic_token(arg) =
  mic_token := string.trim(arg)
  "OK"
end

server.register(namespace="air", description="Station state as JSON", "state", air_state)
server.register(namespace="air", description="Skip the current AutoDJ track", "skip", air_skip)
server.register(namespace="air", usage="live on|off", description="Allow live takeover", "live", air_live)
server.register(namespace="air", usage="record off|live|all", description="Recording mode", "record", air_record)
server.register(namespace="air", usage="volume 0-100", description="Music level under the microphone", "volume", air_volume)
server.register(namespace="air", usage="pause on|off", description="Stop AutoDJ after the current song (talk break)", "pause", air_pause)
server.register(namespace="air", usage="mic_token <token>", description="Token the browser microphone must present", "mic_token", air_mic_token)
