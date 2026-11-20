# Late Stream

Late Stream is a small web app that plays **recorded videos from a Synology NAS** to
**YouTube Live** at a scheduled time. It's for "delayed live" broadcasts.

You set up each broadcast in YouTube Studio (title, thumbnail, Public). In Late Stream
you choose the file on the NAS, the start time, and paste the stream URL and key. At the
start time the app streams the file over RTMP(S). When the stream ends, it switches the
video to **Unlisted**. Email alerts tell you when something goes wrong.

```
 Synology NAS ──File Station API (HTTPS, range requests)──▶ Late Stream ──RTMPS──▶ YouTube Live
 (ProRes files)                                             ffmpeg: ProRes → H.264 1080p30
                                                            web UI · scheduler · email alerts
                                                                 │
                                       YouTube Data API (optional) └─▶ after the stream: set Unlisted
```

## Features

- **Web UI** for settings and for setting up each stream: NAS file browser, date and time
  in your time zone, stream URL and key, an optional YouTube link, and an "unlist after"
  checkbox. Each stream has **Start now**, **Stop**, **Resume** and **Duplicate** buttons.
- **Reads straight from the NAS** through the File Station API. Nothing is copied
  locally, so 80–120 GB files need no extra disk. The app uses HTTP range requests, so
  MOV files with the index at the end work, and it can resume from any point.
- **Live transcoding** of ProRes (Proxy, LT, 422, HQ, 4444) to H.264 1080p30 with AAC
  audio, which is what YouTube's RTMP ingest wants. H.264, HEVC, DNxHD and other common
  codecs are also accepted. Files that are already YouTube-ready are sent without
  re-encoding.
- **Starts on time.** The app checks the file 60 minutes before start (configurable). If
  the check fails, it emails you and keeps retrying every 5 minutes.
- **Recovers on its own:**
  - If the connection to the NAS or YouTube drops, it reconnects from the same point,
    with backoff, for about 18 minutes.
  - If the NAS session expires, it logs in again.
  - If the app restarts mid-stream, it resumes automatically when it was down for less
    than 5 minutes. Otherwise there's a **Resume** button.
- **No overlaps.** The app rejects a stream whose time overlaps another one, using the
  real length of the file.
- **Email alerts.** Failures are always sent: check failed, reconnecting, failed, missed,
  interrupted, or unlisting failed. Started and finished alerts are optional.
- **After the stream** it ends the broadcast if YouTube hasn't already, then sets the video
  to **Unlisted**. This needs YouTube connected; see below. It also starts the broadcast
  if Auto-start was left off in Studio.

## Install

### Docker (recommended)

```bash
git clone … && cd Late-stream
docker compose up -d --build
# open http://<this-machine>:8080
```

All state (settings, schedule, logs) lives in `./data`. That folder holds the stream keys
and passwords, so keep it private.

### Without Docker

You need Python 3.10 or newer and **ffmpeg 5 or newer** on your `PATH`.

```bash
pip install .
late-stream serve --data-dir ./data --port 8080
```

On Linux you can run it as a systemd service: `ExecStart=/usr/local/bin/late-stream serve
--data-dir /var/lib/late-stream`, with `Restart=always`.

### Where to run it

Run it on a machine on the **same LAN as the NAS**, such as a mini PC or a small server.

- **CPU:** live H.264 encoding of 1080p30 needs about **2–4 modern CPU cores**. In a test,
  ProRes Proxy 1080p30 encoded at 3.2× real time on 4 cores. Machines with an Intel CPU
  can use **Quick Sync** (`h264_qsv`), and NVIDIA GPUs can use **NVENC**. Both need
  almost no CPU. Set this in Settings → Encoder.
- **Running on the NAS itself:** most Synology CPUs are too weak to encode 1080p live, so
  only run it in Container Manager on a model with a strong CPU.
- **Network:** the app pulls about 45 Mbps from the NAS (fine on 1 GbE). It sends about
  **8.5 Mbps** to YouTube, so you need **at least 15–20 Mbps of stable upload**. Use a
  wired connection and a UPS.

## First-time setup (in the web UI)

1. **Admin password and time zone.** Set them when the page first opens. Keep the UI on
   your LAN and don't port-forward it.
2. **Settings → Synology NAS.**
   - In DSM, create a user (for example `streamer`). Give it **read-only** access to the
     recordings shared folder and permission for the File Station application.
   - **Turn off 2-step verification** for this user, because the app logs in by itself.
   - Address: `https://<nas-ip>:5001`. Leave "Verify certificate" off if the NAS uses
     Synology's self-signed certificate.
   - Click **Save & test connection**.
3. **Settings → Email alerts.** Enter your SMTP server. For Gmail: `smtp.gmail.com`, port
   587, STARTTLS, and a Google **App password**. Click **Save & send test email**.
4. **Settings → YouTube.** This is optional but needed for the automatic switch to
   Unlisted:
   1. In Google Cloud Console, create a project and enable **YouTube Data API v3**.
   2. On the **OAuth consent screen**, add the scope `.../auth/youtube` and **publish**
      the app. In "Testing" mode Google expires the login every 7 days. You'll see an
      "unverified app" warning once; that's fine for your own channel.
   3. Under **Credentials**, create an OAuth client ID of type **Desktop app** and download
      the JSON.
   4. Upload the JSON, click **Connect YouTube**, and log in as the channel owner.
   5. If the browser then shows "can't be reached", the browser isn't on the same machine
      as the app. Paste the address from the address bar into the box under the button.
5. **Settings → Encoder.** The defaults are H.264 1080p, 8000 kbps, 2 s keyframes and
   160 kbps AAC. They suit ProRes Proxy 1080p30.

## Each stream

1. In **YouTube Studio**, go to Create → Go live → **Schedule stream**. Set the title,
   description and thumbnail, and set **Public**.
2. In the stream settings, turn on **Auto-start** and **Auto-stop**. Pick a stream key;
   a reusable key is fine because the streams never overlap.
3. In **Late Stream**, click **New stream**:
   - Name.
   - **Browse…** the NAS and pick the file.
   - Date and time.
   - Paste the **Stream URL** (`rtmps://a.rtmps.youtube.com/live2` by default) and the
     **Stream key**.
   - Leave "Change the video to Unlisted when the stream ends" ticked. You can paste the
     YouTube link; if you don't, it's found from the stream key.
4. Save. The app checks the file and shows its codec and length. At the start time it goes
   live by itself.

Use **Duplicate** to create the next day's streams quickly. The stream key is copied
over.

## API cost

The **YouTube Data API costs no money**. Google limits it by a quota instead: each project
gets **10,000 units per day**. Late Stream uses the API only after the stream starts, and
only when YouTube is connected:

| Step | Calls | Units |
|---|---|---|
| Find the broadcast for the stream key | `liveStreams.list` + `liveBroadcasts.list` | 2 |
| Check it went live | `liveBroadcasts.list` | 1 |
| Wait for Auto-stop to end it | `liveBroadcasts.list` × 1–10 | 1–10 |
| Set Unlisted | `videos.list` + `videos.update` | 51 |
| *Only if Auto-stop didn't end it* | `liveBroadcasts.transition` → complete | +50 |
| *Only if Auto-start was off* | `liveBroadcasts.transition` (testing/live) | +50–100 |
| **Typical per stream** | | **≈ 55–65** (worst case ≈ 215) |

**3 streams per day ≈ 200 units, about 2% of the free quota.** If you don't connect
YouTube, the app uses 0 units: RTMP streaming itself never touches the API.

The real costs are your own infrastructure:

- **Upload data.** 8.5 Mbps × 4–6 h × 3 streams ≈ **45–70 GB per day**, or about
  1.4–2 TB per month. Check whether your ISP has a data cap.
- **Electricity and hardware** for the machine that runs the app.

## Sizing for your recordings

ProRes Proxy 1080p30 runs at about 45 Mbps, so **80–120 GB lasts about 4–6 hours**.
YouTube only archives live streams of up to **12 hours** as a VOD, so these fit.

## Limits

- **Content ID still applies.** Third-party music can get a stream blocked.
- **Stop button:** stopping a stream doesn't end or unlist the YouTube broadcast. Use
  Resume, or end it in Studio.
- **Secrets:** the stream key and passwords are stored in `data/late-stream.db`, which
  only the app's user can read. The UI never shows the full stream key again after it's
  saved.

## Command-line tools

```bash
late-stream probe FILE_OR_URL     # codec report and how it would be streamed
late-stream prepare IN.mov -o OUT.mp4 --preset medium   # optional offline pre-conversion
```

## Development

```bash
pip install -e '.[dev]' && pytest
```

The tests create small ProRes, H.264 and HEVC clips with ffmpeg. They include a fake
Synology File Station server, which also tests dropped connections and expired sessions.
They push to a real local RTMP server and check the scheduler, recovery, email and
YouTube flows, and the web UI.
