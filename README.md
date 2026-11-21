# GTA clipper

Fully automatic short-form clipping, running free on GitHub Actions:

1. **Every 6 hours** (`make-clips.yml`): checks your streamers for new VODs, transcribes
   the stream, has Claude pick the 10 best moments, cuts them to 1080x1920 with
   captions, a hook and a credit, and puts them in a queue (a GitHub release called `queue`).
2. **4 times a day** (`post.yml`): posts the oldest queued clip to TikTok, Instagram
   Reels and YouTube Shorts through [Upload-Post](https://upload-post.com).

It stops making clips while 16 are already queued, so it only uses runner minutes when needed.

## Setup (about 15 minutes, once)

1. **Create a repo** on GitHub (e.g. `gta-clipper`) and upload these files.
   A **public** repo gets unlimited free Actions minutes. Secrets stay hidden either way.
2. **Upload-Post:** make an account, create a profile, and connect your TikTok,
   Instagram (Business or Creator account) and YouTube channel to it. Put the
   profile name in `config.yaml` → `posting.upload_post_user`. Copy your API key.
3. **Anthropic API key** from console.anthropic.com (picks the moments and writes titles,
   costs a few cents per VOD).
4. In the repo: **Settings → Secrets and variables → Actions → New repository secret**:
   - `ANTHROPIC_API_KEY`
   - `UPLOAD_POST_API_KEY`
   - `YTDLP_COOKIES` *(only if you clip YouTube streams: export youtube.com cookies
     in Netscape format with a browser extension and paste the whole text)*
5. **Settings → Actions → General → Workflow permissions →** "Read and write permissions".
6. Edit `config.yaml`: add your streamers (see the comments in the file).
7. **Actions tab → Make clips → Run workflow** to start the first batch.
   Clips show up under **Releases → queue**. Check a few before posting goes live.
   To post one right away: **Actions → Post clip → Run workflow**.

## Tuning

- **Posting times:** edit the `cron` line in `.github/workflows/post.yml` (times are UTC).
- **Facecam layout:** set `facecam: [x, y, width, height]` per streamer as fractions of
  the frame. Bottom-left cam taking 22% width and 30% height = `[0.0, 0.7, 0.22, 0.3]`.
  The cam goes on top, gameplay below.
- **Re-clip a VOD:** delete its line from `state/processed.json`.
- **Skip a bad clip:** delete its `.mp4` from the `queue` release.

## Good to know

- Twitch and Kick VODs download fine from GitHub. YouTube often blocks GitHub's servers
  without cookies.
- Only clip streamers who allow it (e.g. ones running clipping campaigns on Whop Content
  Rewards). Unlicensed clips get copyright strikes that can delete your accounts.
- TikTok allows 15 API posts per day per account. 4 a day is well inside that.
- GitHub can delay scheduled runs by a few minutes at busy times.
