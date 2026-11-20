# late-stream

Stream **pre-recorded video files** to **YouTube Live** on a schedule (a "delayed live
stream"). The tool uses the YouTube Data API v3 to create the live event and
ffmpeg to push the file over RTMPS in real time.

- Accepts **ProRes** (422 / HQ / LT / Proxy / 4444), **H.264**, **HEVC/H.265**, DNxHD/HR,
  MPEG-2, VP9, AV1, and anything else ffmpeg can decode, in MOV/MP4/MXF/MKV/TS.
- Files that are already YouTube-ready (H.264 8-bit 4:2:0, keyframes ≤ 4 s) are sent
  **without re-encoding** (almost no CPU). Everything else is transcoded live to H.264/AAC.
- Built for large files (80–120 GB). The file is read sequentially at 1× speed and
  never loaded into memory.
- Schedules several streams a day. Each job creates its own YouTube broadcast. The
  stream key is reusable, so it doesn't change between jobs.
- **Self-healing**: if the connection drops or ffmpeg crashes, it restarts from the last
  position (up to 20 times). YouTube keeps the broadcast alive through short gaps.
- Remembers job state, so restarting the service doesn't create duplicate broadcasts.
- Deinterlaces, downscales (for example 4K → 1080p), caps output at 60 fps, merges mono
  audio tracks into stereo, and adds a silent track to files with no audio.

```
recording.mov ──ffprobe──▶ plan (copy | transcode)
                              │
 schedule.yaml ─▶ daemon ─▶ YouTube API: liveStreams (reusable key) + liveBroadcasts.insert + bind
                              │                         at start time
                              └──────────▶ ffmpeg -re ... -f flv rtmps://a.rtmps.youtube.com/live2/<key>
                                               (auto-start → LIVE, at end → complete)
```

## Install

Requirements: Python ≥ 3.10 and ffmpeg/ffprobe ≥ 5 on `PATH`. Any OS works (Linux,
macOS or Windows).

```bash
pip install -e .            # or: pip install -e '.[dev]' to run tests
late-stream --help
```

## One-time Google / YouTube setup

1. **Enable live streaming** on the channel at <https://www.youtube.com/features>. This
   needs phone verification, and the first activation can take **up to 24 hours**.
2. Open the **Google Cloud Console** and create a project. Enable **YouTube Data API v3**.
3. Set up the **OAuth consent screen** (External, or Internal for Workspace). Add the scope
   `https://www.googleapis.com/auth/youtube`, then click **Publish app** to move it to
   *In production*.
   > ⚠️ If the app stays in *Testing*, Google expires the refresh token after **7 days**,
   > and the unattended daemon stops working. An unverified app in production is fine for
   > your own channel. You'll just click through an "unverified app" warning once.
4. Under **Credentials**, create an *OAuth client ID* of type **Desktop app**. Download it
   as `client_secret.json` next to your schedule file.
5. Run `late-stream auth -c schedule.yaml` and log in **as the channel owner or manager**.
   This writes `token.json`. Keep it secret: it gives access to the channel.

## Usage

```bash
cp config.example.yaml schedule.yaml      # edit jobs: file, start (with UTC offset), title
late-stream probe /mnt/rec/session1.mov   # codec report and copy/transcode decision
late-stream check -c schedule.yaml        # probe every job, find overlaps and missing files
late-stream daemon -c schedule.yaml       # run as a service; re-reads the file every 30 s

# single job, now, for testing (unlisted/private recommended):
late-stream run-job -c schedule.yaml 2026-11-21-a --now
# dry run without YouTube: write what would be sent to a local file
late-stream run-job -c schedule.yaml 2026-11-21-a --now --output /tmp/test.flv

# optional: pre-convert ProRes to a small YouTube-ready MP4 ahead of time
late-stream prepare /mnt/rec/session1.mov -o /mnt/rec/session1.mp4 --preset medium
```

To run the daemon as a service on Linux, use systemd:

```ini
# /etc/systemd/system/late-stream.service
[Service]
WorkingDirectory=/opt/late-stream
ExecStart=/usr/local/bin/late-stream daemon -c /opt/late-stream/schedule.yaml
Restart=always
User=streamer
[Install]
WantedBy=multi-user.target
```

Logs go to `logs/late-stream.log`, and each job writes its ffmpeg log to `logs/<job-id>/`.
`state.json` shows each job's status (`scheduled`, `live`, `done`, `failed` or `missed`)
and its watch URL.

## API cost

**The YouTube Data API costs no money.** Google limits it by a quota instead: each
project gets **10,000 units per day** (resets at midnight Pacific time). You can request
more for free through the quota extension form, which involves a compliance audit.

| Call | Units | Per stream |
|---|---|---|
| `liveStreams.list` (find the reusable key) | 1 | 1 |
| `liveStreams.insert` (first time only, key reused afterwards) | 50 | 0 |
| `liveBroadcasts.insert` (create event) | 50 | 50 |
| `liveBroadcasts.bind` | 50 | 50 |
| `thumbnails.set` (optional) | 50 | 0–50 |
| `liveBroadcasts.list` (status checks) | 1 | ~2 |
| `liveBroadcasts.transition` → complete | 50 | 50 |
| `videos.update` (only if `category_id` set) | 50 | 0–50 |
| manual go-live (`auto_start: false`): polls + transition | 1 each + 50 | ~0–90 |
| **Typical total** | | **≈ 150–300** |

**3 streams/day ≈ 450–900 units/day, which is under 10% of the free quota.** You never
need to pay for quota. Video ingest, transcoding on YouTube's side and archiving the
stream as a VOD are also free.

The real costs are your own infrastructure:

| Item | Estimate |
|---|---|
| Upload bandwidth | 1080p: 6–9 Mbps per stream (have ≥ 20 Mbps of stable upload). 4K: 25–35 Mbps (have ≥ 60 Mbps). |
| Data sent | ~3–4 GB per hour of 1080p. With 3 streams/day that's ≈ 10–15 GB/day. |
| Cloud VM egress (if used) | ≈ $0.08–0.12/GB → ≈ $1–2/day for 3 × 1 h at 1080p |
| Storage | 3 × 80–120 GB = **240–360 GB/day**, or ≈ 7–11 TB/month if you keep the masters |
| Compute | See sizing below |

## Sizing for 80–120 GB recordings

How long a file lasts depends on the codec:

| Source | Bitrate | 100 GB lasts |
|---|---|---|
| ProRes 422 HQ 1080p30 | ~220 Mbps | ~1 h |
| ProRes 422 1080p30 | ~147 Mbps | ~1.5 h |
| ProRes 422 HQ 2160p30 | ~707 Mbps | ~20 min |
| H.264 50 Mbps (camera) | 50 Mbps | ~4.4 h |

- **Disk read** at 1× speed is ≤ 90 MB/s even for 4K ProRes HQ, so any SSD, RAID or 10 GbE
  NAS works. A 1 GbE NAS handles 1080p ProRes but is tight for 4K ProRes HQ.
- **CPU for live transcoding** with `libx264 veryfast`: 1080p30 needs about 4–6 modern
  cores. 4K→1080p from ProRes needs about 8+ cores. For 4K output, or to leave plenty of
  headroom, use a GPU (`video_encoder: h264_nvenc` on NVIDIA, `h264_qsv` on Intel, or
  `h264_videotoolbox` on a Mac). The log warns if encoding falls below real time.
- **Most robust option:** run `late-stream prepare` on the ProRes file as soon as the
  recording ends. A 1 h ProRes HQ file (~100 GB) becomes a ~3–5 GB H.264 file that streams
  with `-c copy` and needs almost no CPU. That also makes it practical to stream from a
  cheap cloud VM, because you upload 4 GB instead of 100 GB.
- Run the machine on a wired connection with a UPS. Keep its clock NTP-synced.

## Notes and limits

- YouTube only archives a live stream as a VOD if it's **12 hours or shorter**.
- Rerunning pre-recorded content as "live" is allowed. **Content ID still applies**, and
  third-party music can get the stream cut off or blocked.
- If two jobs overlap, give them different `ingest_stream` names. `check` catches this.
- `auto_start: true` makes the event go live as soon as video arrives at the start time.
  `latency: normal` gives the best quality and enables DVR.

## Development

```bash
pip install -e '.[dev]' && pytest     # builds tiny ProRes/H.264/HEVC test clips with ffmpeg
```
