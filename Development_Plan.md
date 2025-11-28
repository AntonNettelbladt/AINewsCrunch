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
  - Run local auth script once to obtain a refresh token
  - Store secrets as:
    - `YOUTUBE_CLIENT_ID`
    - `YOUTUBE_CLIENT_SECRET`
    - `YOUTUBE_REFRESH_TOKEN`

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
  - `google-api-python-client`, `oauth2client`
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
  - YouTube uploader: refresh token → access token → upload MP4 with metadata
  - TikTok uploader: use access token, prefer pull-from-URL or chunked upload
  - Error handling: Log failures, send notifications (optional: email/webhook)

### Phase 2 – Repository Structure

```
/
├── bot.py
├── requirements.txt
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

### Phase 4 – Publishing Core (Outline)

```python
def rank_stories(articles):
    # Score each article based on:
    # - High-impact keywords (AI, breakthrough, launch, acquisition, etc.)
    # - Recency (bonus for < 24 hours)
    # - Source authority (major publications get higher score)
    # - Article length (need enough content)
    # Return: sorted list of articles by impact score

def select_top_story(ranked_articles):
    # Pick the single highest-scoring story
    # Ensure it has usable image and sufficient content
    # Return: single article object

def generate_script(article):
    # Use Google Gemini API to create engaging 30-60 second script
    # Focus ENTIRELY on the single selected story
    # Include hook, 2-3 key points, why it matters, call-to-action
    # Fallback to template-based summarizer if Gemini API fails
    # Return: script text, estimated duration

def fetch_stock_media(keywords, media_type='photo'):
    # Search Pexels/Pixabay/Unsplash APIs for free stock media
    # Extract keywords from article title/summary
    # Return: URL to high-quality stock image/video matching story theme
    # Fallback order: Pexels → Pixabay → Unsplash → placeholder

def generate_metadata(article, script):
    # Create SEO-friendly title (under 100 chars) focused on the story
    # Generate description with hashtags related to the story
    # Extract relevant tags from the article
    # Return: dict with title, description, tags

def upload_to_youtube(video_path, title, description, tags):
    # 1. Exchange refresh token for access token
    # 2. Build youtube service via google-api-python-client
    # 3. Prepare metadata (title, description, tags, category='22', privacyStatus='public')
    # 4. Upload video with resumable upload for large files
    # 5. Log returned video ID and handle errors

def upload_to_tiktok(video_path, title):
    # 1. POST /v2/post/publish/video/init/ to obtain upload_url
    # 2. PUT video bytes (chunk if required) to upload_url
    # 3. Poll posting status until published
    # 4. Handle TikTok-specific errors (file size, duration limits)
```

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
- [ ] Local script produces `.mp4` (1080x1920, 30-60 seconds)
- [ ] Story ranking algorithm tested and selects high-impact stories
- [ ] Single-story focus verified (script covers one story only)
- [ ] Video file size under 50MB (TikTok limit)
- [ ] YouTube OAuth flow completed; refresh token stored
- [ ] TikTok access token secured (all three: CLIENT_KEY, CLIENT_SECRET, ACCESS_TOKEN)
- [ ] GitHub Secrets populated (YouTube, TikTok, optional stock media API keys)
- [ ] `requirements.txt` committed with all dependencies
- [ ] Workflow YAML committed under `.github/workflows/`
- [ ] Error handling and logging implemented
- [ ] Test with `workflow_dispatch` (manual trigger)

### Post-Deployment
- [ ] Monitor first automated run (check Actions logs)
- [ ] Verify video appears on YouTube and TikTok
- [ ] Check video quality and metadata
- [ ] Set up notifications (optional: email/webhook on failure)
- [ ] Document any manual intervention needed