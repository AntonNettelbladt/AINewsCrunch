Development Plan: Serverless AI Video Automation System
======================================================

- **Target Platform:** GitHub Actions (Free Tier)
- **Output Format:** Vertical Video (9:16) for YouTube Shorts / TikTok
- **Cost Estimate:** $0/month (Execution) + API costs (Free Tiers: Gemini API free tier, Google Cloud TTS free tier)

## 1. Executive Summary

Build a daily "text-to-video" tech news pipeline that runs entirely inside GitHub Actions. Each run scrapes technology headlines with Newspaper3k, **selects the single most impactful story** based on engagement potential, drafts a focused script for that one story, generates narration, gathers or creates visuals, edits a vertical video, and publishes directly to YouTube and TikTok. The runner cleans itself up after completion, keeping infrastructure costs near zero.

## 2. System Architecture

- **Trigger:** GitHub Actions cron (`0 10 * * *`, 10:00 AM UTC)
- **Runner:** `ubuntu-latest` (max 6 hours execution time)
- **Logic Core (Python)**
  - News source: Newspaper3k scraping (RSS + article parsing)
  - **Story selection:** Rank articles by impact score (keywords, recency, source authority, engagement signals)
  - **Single-story focus:** Select top-ranked story for deep-dive video
  - Script generation: Google Gemini API (gemini-pro model) with fallback to template-based summarizer
  - Audio: Google Cloud Text-to-Speech API (Neural2 voices) with fallback to Edge-TTS
  - Visuals: Article images (resized) + free stock media fallback (Pexels, Pixabay, Unsplash APIs)
  - Stock media: Fetch free stock photos/videos when article images unavailable or low quality
  - Assembly: MoviePy (1080x1920, 15-60 seconds, Ken Burns effect, captions)
  - Metadata: Auto-generate titles, descriptions, tags (optimized for single-story SEO)
  - Publishing: YouTube Data API + TikTok Content Posting API
  - Error handling: Logging, optional notifications
- **Cleanup:** Runner discards temporary data after the job
- **Constraints:**
  - Video duration: 15-60 seconds (platform limits)
  - File size: Max 50MB (TikTok limit)
  - Resolution: 1080x1920 (9:16 aspect ratio)

## 3. Prerequisites & Access

### GitHub
- Repository: preferably private to hide logs
- Secrets: `Settings > Secrets and variables > Actions`

### External Data Sources

- **Tech News Websites**
  - Curate RSS feeds or site URLs (e.g., The Verge, TechCrunch, Wired)
  - Verify each site permits scraping for personal projects (respect robots.txt)
  - No API keys required when using Newspaper3k

- **Free Stock Media APIs** (Fallback for visuals)
  - **Pexels API**: Free photos and videos (requires free API key)
    - Sign up at pexels.com/api
    - Store `PEXELS_API_KEY` (optional, but recommended)
  - **Pixabay API**: Free images and videos (requires free API key)
    - Sign up at pixabay.com/api/docs
    - Store `PIXABAY_API_KEY` (optional, but recommended)
  - **Unsplash API**: Free high-quality photos (requires free API key)
    - Sign up at unsplash.com/developers
    - Store `UNSPLASH_API_KEY` (optional, but recommended)
  - Note: All three offer free tiers with generous rate limits for personal projects

- **YouTube Data API (OAuth 2.0)**
  - Create Google Cloud project; enable YouTube Data API v3
  - Generate OAuth client (Desktop or Other)
  - Run `youtube_oauth.py` helper script once to obtain a refresh token ✅ **HELPER SCRIPT CREATED**
  - Store secrets as:
    - `YT_CLIENT_ID`
    - `YT_CLIENT_SECRET`
    - `YT_REFRESH_TOKEN`

- **TikTok Content Posting API**
  - Create developer account and app
  - Enable Content Posting API with `video.publish` scope
  - Complete OAuth to gain user access token
  - Store secrets as:
    - `TIKTOK_CLIENT_KEY`
    - `TIKTOK_CLIENT_SECRET`
    - `TIKTOK_ACCESS_TOKEN`

- **Google Gemini API** (for script generation)
  - Get API key from Google AI Studio: https://makersuite.google.com/app/apikey
  - Free tier: 60 requests/minute, 1,500 requests/day
  - Store as: `GEMINI_API_KEY`

- **Google Cloud Text-to-Speech API** (for voiceover)
  - Create Google Cloud project
  - Enable Cloud Text-to-Speech API
  - Create service account and download JSON credentials
  - Free tier: 0-4 million characters/month (varies by region)
  - Store service account JSON path as: `GOOGLE_APPLICATION_CREDENTIALS`
  - Or use Application Default Credentials if running on Google Cloud

## 4. Implementation Phases

### Phase 1 – Local Development (`bot.py`)

- **Goal:** Core script runs locally end-to-end.
- **Tech Stack:**
  - Python 3.10+
  - `requests`, `moviepy==1.0.3`, `edge-tts`
  - `google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2`, `oauth2client`
  - `imageio[ffmpeg]`
  - `Pillow` (image processing)
  - `python-dotenv` (local development)
  - No paid APIs required (fully free implementation)
- **Key Blocks:**
  - News fetcher: use Newspaper3k to pull articles from curated tech RSS feeds, deduplicate
  - **Story ranker:** Score each article by:
    - High-impact keywords (AI breakthrough, major launch, acquisition, controversy)
    - Recency (prefer stories < 24 hours old)
    - Source authority (prioritize major tech publications)
    - Article length (sufficient content for 30-60s script)
    - Engagement signals (if available: shares, comments, trending status)
  - **Story selector:** Pick the single highest-scoring story for the video
  - Script generator: Use Google Gemini API to create engaging 30-60 second script **focused entirely on the selected story** (hook, key details, why it matters), with fallback to template-based summarizer
  - Sanitizer: clean article text, remove special characters
  - Image fetcher: Download article images, resize to 1080x1920, handle failures
  - Stock media fetcher: If article image fails or is low quality, search and download from Pexels/Pixabay/Unsplash using story keywords
  - Video clip fetcher (optional): Download short stock video clips for B-roll if available
  - Audio generator: Google Cloud TTS with Neural2 voice, save as MP3, with fallback to Edge-TTS
  - Video editor: enforce 1080x1920, 30-60 second duration, Ken Burns effect, captions
  - Metadata generator: Create SEO-friendly title, description, tags (all focused on the single story)
  - YouTube uploader: refresh token → access token → upload MP4 with metadata ✅ **COMPLETE**
  - TikTok uploader: use access token, prefer pull-from-URL or chunked upload ✅ **COMPLETE**
  - Error handling: Log failures, send notifications (optional: email/webhook) ✅ **COMPLETE**

### Phase 2 – Repository Structure

```
/
├── bot.py
├── requirements.txt
├── youtube_oauth.py      # OAuth helper script for YouTube token generation
├── fonts/                # optional custom TTFs
└── .github/
    └── workflows/
        └── daily_video.yml
```

`requirements.txt`

```
moviepy==1.0.3
edge-tts
requests
google-api-python-client
google-auth-oauthlib>=0.5.0  # For YouTube OAuth flow
google-auth-httplib2>=0.1.0  # For OAuth HTTP transport
oauth2client
imageio[ffmpeg]
Pillow>=10.0.0
python-dotenv
newspaper3k
# Stock media APIs accessed via requests (no additional packages needed)
# Pexels, Pixabay, Unsplash all provide REST APIs
```

### Phase 3 – GitHub Actions Workflow (`.github/workflows/daily_video.yml`)

```
name: Daily Tech News Generator

on:
  schedule:
    - cron: '0 10 * * *' # 10 AM UTC
  workflow_dispatch:

jobs:
  build-and-post:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Install FFmpeg & ImageMagick
        run: |
          sudo apt-get update
          sudo apt-get install -y ffmpeg imagemagick
          sudo sed -i 's/none/read,write/g' /etc/ImageMagick-6/policy.xml

      - uses: actions/setup-python@v4
        with:
          python-version: '3.10'

      - run: pip install -r requirements.txt

      - name: Run generator
        env:
          YT_CLIENT_ID: ${{ secrets.YOUTUBE_CLIENT_ID }}
          YT_CLIENT_SECRET: ${{ secrets.YOUTUBE_CLIENT_SECRET }}
          YT_REFRESH_TOKEN: ${{ secrets.YOUTUBE_REFRESH_TOKEN }}
          TIKTOK_CLIENT_KEY: ${{ secrets.TIKTOK_CLIENT_KEY }}
          TIKTOK_CLIENT_SECRET: ${{ secrets.TIKTOK_CLIENT_SECRET }}
          TIKTOK_ACCESS_TOKEN: ${{ secrets.TIKTOK_ACCESS_TOKEN }}
          PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}  # Optional: for stock media fallback
          PIXABAY_API_KEY: ${{ secrets.PIXABAY_API_KEY }}  # Optional: for stock media fallback
          UNSPLASH_API_KEY: ${{ secrets.UNSPLASH_API_KEY }}  # Optional: for stock media fallback
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}  # Required: for script generation
          GOOGLE_APPLICATION_CREDENTIALS: ${{ secrets.GOOGLE_APPLICATION_CREDENTIALS }}  # Optional: for TTS
          GCLOUD_TTS_VOICE: ${{ secrets.GCLOUD_TTS_VOICE }}  # Optional: TTS voice (default: en-US-Neural2-D)
          GEMINI_MODEL: ${{ secrets.GEMINI_MODEL }}  # Optional: Gemini model (default: gemini-pro)
        run: python bot.py
```

### Phase 4 – Publishing Core ✅ **COMPLETE**

**Status:** All publishing functions have been implemented and integrated into the main pipeline.

**Implemented Functions:**

- ✅ `rank_stories()` - Implemented as `rank_articles()` in `bot.py`
- ✅ `select_top_story()` - Implemented in `bot.py`
- ✅ `generate_script()` - Implemented with Gemini API and template fallback
- ✅ `fetch_stock_media()` - Implemented with Pexels/Pixabay/Unsplash support
- ✅ `generate_metadata()` - Implemented with SEO-optimized titles, descriptions, and tags
- ✅ `upload_to_youtube()` - **IMPLEMENTED** in `bot.py`
  - OAuth 2.0 token refresh
  - Resumable upload with progress tracking
  - Error handling and retry logic
  - Returns video ID on success
- ✅ `upload_to_tiktok()` - **IMPLEMENTED** in `bot.py`
  - Three-step upload process (init → upload → poll)
  - Chunked upload for large files
  - Status polling until published
  - Error handling for rate limits and file size validation

**Additional Files Created:**
- ✅ `youtube_oauth.py` - Helper script for generating YouTube refresh tokens

**Integration:**
- ✅ Upload functions integrated into `main()` pipeline
- ✅ Graceful error handling (pipeline continues even if uploads fail)
- ✅ Comprehensive logging for upload success/failure

## 5. Risk Management & Limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| YouTube token expiry | Upload fails months later | Re-run OAuth consent to refresh token; verify app status |
| TikTok audit | Videos remain private | Submit app for TikTok audit to unlock public posting |
| Source website blocks scraping | Unable to fetch articles | Rotate list of sources, respect robots.txt, add retries/backoff |
| FFmpeg errors | Video build fails | Pin dependency versions and use `imageio-ffmpeg` |
| No news articles found | Pipeline fails | Implement fallback: use cached articles or skip day |
| No high-impact stories | Video may get low views | Lower threshold or expand keyword list; ensure at least one story meets minimum score |
| Selected story lacks depth | Script too short | Require minimum article length (500+ words) in ranking criteria |
| Video too large | Upload fails | Compress video, enforce max file size (50MB for TikTok) |
| Video duration limits | Rejected by platforms | Ensure 15-60 seconds (YouTube Shorts: 15-60s, TikTok: 15-180s) |
| GitHub Actions timeout | Job fails | Optimize video processing; consider splitting into multiple jobs |
| Missing images | Visual quality poor | Fallback to free stock media (Pexels/Pixabay/Unsplash) using story keywords; final fallback to solid color backgrounds |
| Stock media API rate limits | Unable to fetch fallback images | Implement caching, rotate between APIs, respect rate limits with exponential backoff |
| Script generation fails | No content | Fallback to template-based script if Gemini API fails |
| Gemini API rate limits | Script generation fails | Implement exponential backoff, fallback to template |
| Google Cloud TTS quota exceeded | Audio generation fails | Fallback to Edge-TTS, monitor character usage |
| Google API authentication fails | Script/audio generation fails | Verify API keys and credentials, check service account permissions |

## 6. Deployment Checklist

### Pre-Deployment
- [x] Local script produces `.mp4` (1080x1920, 30-60 seconds)
- [x] Story ranking algorithm tested and selects high-impact stories
- [x] Single-story focus verified (script covers one story only)
- [x] Video file size under 50MB (TikTok limit)
- [x] YouTube uploader implemented (`upload_to_youtube()` function)
- [x] TikTok uploader implemented (`upload_to_tiktok()` function)
- [x] YouTube OAuth helper script created (`youtube_oauth.py`)
- [ ] YouTube OAuth flow completed; refresh token stored (manual step - use `youtube_oauth.py`)
- [ ] TikTok access token secured (all three: CLIENT_KEY, CLIENT_SECRET, ACCESS_TOKEN) (manual step)
- [x] GitHub Secrets populated (YouTube, TikTok, optional stock media API keys)
- [x] `requirements.txt` committed with all dependencies (including OAuth libraries)
- [x] Workflow YAML committed under `.github/workflows/`
- [x] Error handling and logging implemented
- [x] Upload functions integrated into main pipeline
- [ ] Test with `workflow_dispatch` (manual trigger)

### Post-Deployment
- [ ] Monitor first automated run (check Actions logs)
- [ ] Verify video appears on YouTube and TikTok
- [ ] Check video quality and metadata
- [ ] Set up notifications (optional: email/webhook on failure)
- [ ] Document any manual intervention needed