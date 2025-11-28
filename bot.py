import json
import logging
import os
import random
import re
import tempfile
import textwrap
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from moviepy.editor import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    TextClip,
    VideoFileClip,
    concatenate_videoclips,
)
from newspaper import Article
from PIL import Image, ImageDraw, ImageFilter

# Compatibility shim for MoviePy with Pillow >= 10.0.0
# MoviePy uses Image.ANTIALIAS which was removed in Pillow 10.0.0
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS

try:
    import edge_tts
except ImportError:  # pragma: no cover - optional dependency
    edge_tts = None

try:
    import google.generativeai as genai
except ImportError:  # pragma: no cover - optional dependency
    genai = None

try:
    from google.cloud import texttospeech
except ImportError:  # pragma: no cover - optional dependency
    texttospeech = None

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError
except ImportError:  # pragma: no cover - optional dependency
    Credentials = None
    InstalledAppFlow = None
    Request = None
    build = None
    MediaFileUpload = None
    HttpError = None


@dataclass
class SourceFeed:
    name: str
    rss_url: Optional[str] = None
    html_url: Optional[str] = None  # For direct HTML scraping
    source_type: str = "rss"  # "rss", "reddit", "hackernews", "googlenews", "html"
    weight: float = 1.0
    search_query: Optional[str] = None  # For search-based sources
    headers: Dict[str, str] = field(default_factory=dict)  # Custom headers


@dataclass
class ArticleCandidate:
    title: str
    url: str
    summary: str
    text: str
    image_url: Optional[str]
    published: Optional[datetime]
    source: str
    score: float = 0.0


@dataclass
class Config:
    output_dir: Path
    max_articles: int = 10
    max_script_points: int = 3
    max_script_words: int = 150  # Max words for 60-second script (~2.5 words/second)
    tts_voice: str = "en-US-AriaNeural"  # Fallback for Edge-TTS
    pexels_api_key: Optional[str] = None
    pixabay_api_key: Optional[str] = None
    unsplash_api_key: Optional[str] = None
    ai_only_mode: bool = True
    min_ai_keywords: int = 1
    ai_keyword_boost: float = 2.0
    min_ai_score: float = 5.0  # Minimum score threshold for AI articles
    min_ai_density: float = 0.3  # Minimum AI keyword density (0.3% of words)
    # Google API settings
    gemini_api_key: Optional[str] = None
    gcloud_tts_credentials_path: Optional[str] = None
    gcloud_tts_voice_name: str = "en-US-Neural2-D"
    gemini_model: str = "gemini-pro"
    use_gemini: bool = True
    use_gcloud_tts: bool = True
    # YouTube API settings
    youtube_client_id: Optional[str] = None
    youtube_client_secret: Optional[str] = None
    youtube_refresh_token: Optional[str] = None
    youtube_channel_name: Optional[str] = None  # Channel name or handle to upload to (e.g., "Code Rush" or "@CodeRush_AI")
    upload_to_youtube: bool = True
    # TikTok API settings
    tiktok_client_key: Optional[str] = None
    tiktok_client_secret: Optional[str] = None
    tiktok_access_token: Optional[str] = None
    upload_to_tiktok: bool = True


DEFAULT_SOURCES: List[SourceFeed] = [
    # Tier 1: AI-Focused Sources (Highest Priority)
    SourceFeed(
        name="Google News: AI & Machine Learning",
        source_type="googlenews",
        search_query="artificial intelligence OR AI OR machine learning OR deep learning",
        weight=2.0,
    ),
    SourceFeed(
        name="Google News: LLMs & Models",
        source_type="googlenews",
        search_query="LLM OR GPT OR Claude OR Gemini OR large language model",
        weight=2.0,
    ),
    SourceFeed(
        name="Reddit: MachineLearning",
        source_type="reddit",
        weight=1.8,
    ),
    SourceFeed(
        name="Reddit: artificial",
        source_type="reddit",
        weight=1.8,
    ),
    SourceFeed(
        name="Reddit: ChatGPT",
        source_type="reddit",
        weight=1.7,
    ),
    SourceFeed(
        name="Reddit: singularity",
        source_type="reddit",
        weight=1.6,
    ),
    SourceFeed(
        name="Hacker News AI Stories",
        source_type="hackernews",
        weight=1.6,
    ),
    
    # Tier 2: Direct AI News Sites
    SourceFeed(
        name="MIT Technology Review",
        rss_url="https://www.technologyreview.com/feed/",
        source_type="rss",
        weight=1.5,
    ),
    SourceFeed(
        name="VentureBeat AI",
        rss_url="https://venturebeat.com/ai/feed/",
        source_type="rss",
        weight=1.5,
    ),
    SourceFeed(
        name="The Decoder",
        rss_url="https://www.the-decoder.com/feed/",
        source_type="rss",
        weight=1.4,
    ),
    SourceFeed(
        name="AI News",
        rss_url="https://www.artificialintelligence-news.com/feed/",
        source_type="rss",
        weight=1.4,
    ),
    
    # Tier 3: General Tech (Filtered for AI)
    SourceFeed(
        name="TechCrunch",
        rss_url="https://techcrunch.com/feed/",
        source_type="rss",
        weight=1.2,
    ),
    SourceFeed(
        name="Ars Technica",
        rss_url="https://feeds.arstechnica.com/arstechnica/index",
        source_type="rss",
        weight=1.0,
    ),
    SourceFeed(
        name="Wired",
        rss_url="https://www.wired.com/feed/rss",
        source_type="rss",
        weight=1.0,
    ),
]

EXCLUSION_KEYWORDS = {
    # Sales terms
    "black friday", "cyber monday", "sale", "deal", "discount", "coupon", "promo", 
    "save", "cheap", "bargain", "on sale", "clearance", "flash sale",
    # Product review terms
    "review", "hands-on", "unboxing", "first look", "buyer's guide", "best ", 
    "top ", "ranking", "comparison", "vs ", "versus",
    # Shopping terms
    "where to buy", "price", "cost", "affordable", "budget", "pricing",
    "buy now", "shop", "shopping", "purchase", "order",
    # Deals/offers
    "limited time", "special offer", "flash sale", "clearance", "promotion",
    "get it now", "order now", "buy it", "add to cart",
    # Low-value content indicators
    "sponsored", "advertisement", "ad", "sponsor", "promoted",
}

MAJOR_NEWS_INDICATORS = {
    # Breakthroughs (highest weight)
    "breakthrough": 4.0,
    "revolutionary": 3.5,
    "game-changing": 3.5,
    "milestone": 3.0,
    "first-of-its-kind": 3.5,
    "groundbreaking": 3.5,
    # Launches (high weight)
    "launch": 3.0,
    "release": 3.0,
    "announcement": 2.5,
    "unveiled": 3.0,
    "introduced": 2.5,
    "debut": 2.5,
    "unveiling": 3.0,
    # Acquisitions (high weight)
    "acquisition": 3.5,
    "merger": 3.5,
    "bought": 3.0,
    "acquired": 3.5,
    "purchased": 3.0,
    "takeover": 3.0,
    # Research (high weight)
    "research": 2.5,
    "study": 2.5,
    "paper": 2.5,
    "published": 2.5,
    "findings": 2.5,
    "discovery": 3.0,
    "scientific": 2.5,
    # Partnerships (medium-high weight)
    "partnership": 2.5,
    "collaboration": 2.5,
    "teams up": 2.5,
    "joins forces": 2.5,
    "alliance": 2.5,
    # Controversies (high weight - generates views)
    "controversy": 3.0,
    "lawsuit": 3.0,
    "legal": 2.5,
    "regulation": 2.5,
    "ban": 3.0,
    "restriction": 2.5,
    "lawsuit": 3.0,
    "sued": 3.0,
    # Funding (medium-high weight)
    "funding": 2.5,
    "investment": 2.5,
    "raised": 3.0,
    "valuation": 2.5,
    "ipo": 3.0,
    "venture capital": 2.5,
    "series": 2.5,  # Series A, B, C funding
}

AI_KEYWORDS = {
    # Core AI terms (high weight)
    "artificial intelligence": 3.0,
    "ai": 3.0,
    "machine learning": 2.5,
    "ml": 2.5,
    "deep learning": 2.5,
    "neural network": 2.5,
    "neural networks": 2.5,
    
    # AI companies (high weight)
    "openai": 2.8,
    "anthropic": 2.5,
    "google ai": 2.3,
    "microsoft ai": 2.3,
    "meta ai": 2.3,
    "deepmind": 2.5,
    "stability ai": 2.0,
    "midjourney": 2.0,
    
    # AI models (high weight)
    "gpt": 2.8,
    "gpt-4": 3.0,
    "gpt-3": 2.5,
    "claude": 2.8,
    "gemini": 2.8,
    "llm": 2.5,
    "large language model": 2.8,
    "large language models": 2.8,
    "chatgpt": 2.8,
    "dall-e": 2.0,
    "stable diffusion": 2.0,
    
    # AI applications (medium-high weight)
    "chatbot": 2.0,
    "generative ai": 2.5,
    "computer vision": 2.0,
    "nlp": 2.0,
    "natural language processing": 2.3,
    "ai assistant": 2.0,
    "ai agent": 2.0,
    "ai tool": 2.0,
    "ai system": 2.0,
    
    # AI events and breakthroughs (high weight)
    "ai breakthrough": 3.0,
    "ai model": 2.5,
    "ai system": 2.0,
    "ai research": 2.0,
    "ai development": 2.0,
    "ai innovation": 2.5,
    
    # Additional AI-related terms
    "transformer": 2.0,
    "transformer model": 2.3,
    "reinforcement learning": 2.0,
    "supervised learning": 1.8,
    "unsupervised learning": 1.8,
    "ai training": 1.8,
    "model training": 1.8,
    
    # AI-user-focused keywords (high weight for developers/AI users)
    "coding agent": 3.5,
    "ai agent": 3.0,  # Updated weight
    "copilot": 3.2,
    "github copilot": 3.5,
    "cursor ai": 3.3,
    "claude code": 3.3,
    "code generation": 3.0,
    "ai coding": 3.0,
    "ai developer": 2.8,
    "ai programming": 2.8,
    "llm update": 3.0,
    "model update": 3.0,
    "new model": 3.0,
    "model release": 3.2,
    "new gpt": 3.2,
    "gpt-5": 3.5,
    "claude update": 3.2,
    "gemini update": 3.2,
    "new feature": 2.5,
    "ai feature": 3.0,
    "beta": 2.0,
    "preview": 2.0,
    "api": 2.5,
    "sdk": 2.5,
    "integration": 2.3,
    "prompt": 2.0,
    "prompting": 2.0,
    "fine-tuning": 2.5,
    "fine tuning": 2.5,
    "training": 2.0,
    "inference": 2.3,
    "token": 2.0,
    "context window": 2.5,
    "multimodal": 2.5,
    "vision model": 2.8,
    "code model": 3.0,
    "ai assistant": 2.5,  # Updated weight
}


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def setup_nltk() -> None:
    """Download required NLTK data for newspaper3k."""
    try:
        import nltk
        # Download punkt_tab tokenizer (required for newspaper3k)
        try:
            nltk.data.find('tokenizers/punkt_tab')
        except LookupError:
            logging.info("Downloading NLTK punkt_tab tokenizer...")
            nltk.download('punkt_tab', quiet=True)
            logging.info("NLTK punkt_tab tokenizer downloaded successfully")
    except ImportError:
        logging.warning("NLTK not available, newspaper3k may have issues parsing articles")
    except Exception as exc:
        logging.warning("Failed to setup NLTK: %s", exc)


def load_config() -> Config:
    if load_dotenv:
        load_dotenv()

    output_dir = Path(os.getenv("OUTPUT_DIR", "artifacts"))
    output_dir.mkdir(parents=True, exist_ok=True)

    # Debug: Check if credentials are being read (without exposing values)
    yt_client_id = os.getenv("YT_CLIENT_ID")
    yt_client_secret = os.getenv("YT_CLIENT_SECRET")
    yt_refresh_token = os.getenv("YT_REFRESH_TOKEN")
    tiktok_client_key = os.getenv("TIKTOK_CLIENT_KEY")
    tiktok_client_secret = os.getenv("TIKTOK_CLIENT_SECRET")
    tiktok_access_token = os.getenv("TIKTOK_ACCESS_TOKEN")
    gcloud_creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    
    logging.info("YouTube credentials present: client_id=%s, client_secret=%s, refresh_token=%s", 
                 "yes" if yt_client_id else "no",
                 "yes" if yt_client_secret else "no", 
                 "yes" if yt_refresh_token else "no")
    logging.info("TikTok credentials present: client_key=%s, client_secret=%s, access_token=%s",
                 "yes" if tiktok_client_key else "no",
                 "yes" if tiktok_client_secret else "no",
                 "yes" if tiktok_access_token else "no")
    logging.info("Google Cloud TTS credentials present: %s", "yes" if gcloud_creds else "no")

    config = Config(
        output_dir=output_dir,
        max_script_points=int(os.getenv("MAX_SCRIPT_POINTS", "3")),
        max_script_words=int(os.getenv("MAX_SCRIPT_WORDS", "150")),
        tts_voice=os.getenv("TTS_VOICE", "en-US-AriaNeural"),
        pexels_api_key=os.getenv("PEXELS_API_KEY"),
        pixabay_api_key=os.getenv("PIXABAY_API_KEY"),
        unsplash_api_key=os.getenv("UNSPLASH_API_KEY"),
        ai_only_mode=os.getenv("AI_ONLY_MODE", "true").lower() == "true",
        min_ai_keywords=int(os.getenv("MIN_AI_KEYWORDS", "1")),
        ai_keyword_boost=float(os.getenv("AI_KEYWORD_BOOST", "2.0")),
        min_ai_score=float(os.getenv("MIN_AI_SCORE", "5.0")),
        min_ai_density=float(os.getenv("MIN_AI_DENSITY", "0.3")),
        # Google API settings
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        gcloud_tts_credentials_path=gcloud_creds,
        gcloud_tts_voice_name=os.getenv("GCLOUD_TTS_VOICE", "en-US-Neural2-D"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-pro"),
        use_gemini=os.getenv("USE_GEMINI", "true").lower() == "true",
        use_gcloud_tts=os.getenv("USE_GCLOUD_TTS", "true").lower() == "true",
        # YouTube API settings
        youtube_client_id=yt_client_id,
        youtube_client_secret=yt_client_secret,
        youtube_refresh_token=yt_refresh_token,
        youtube_channel_name=os.getenv("YT_CHANNEL_NAME", "Code Rush"),  # Default to "Code Rush"
        upload_to_youtube=os.getenv("UPLOAD_TO_YOUTUBE", "true").lower() == "true",
        # TikTok API settings
        tiktok_client_key=tiktok_client_key,
        tiktok_client_secret=tiktok_client_secret,
        tiktok_access_token=tiktok_access_token,
        upload_to_tiktok=os.getenv("UPLOAD_TO_TIKTOK", "true").lower() == "true",
    )
    logging.debug("Loaded config: %s", config)
    return config


# User agents for better scraping
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]


def get_headers(custom_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Get default headers with random user agent, optionally merged with custom headers."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if custom_headers:
        headers.update(custom_headers)
    return headers


def fetch_with_retry(url: str, max_retries: int = 3, headers: Optional[Dict[str, str]] = None, timeout: int = 15) -> Optional[requests.Response]:
    """Fetch URL with retry logic and proper headers."""
    if headers is None:
        headers = get_headers()
    
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) + random.uniform(0, 1)
                logging.debug("Retry %d/%d for %s after %.1fs: %s", attempt + 1, max_retries, url, wait_time, exc)
                time.sleep(wait_time)
            else:
                logging.warning("Failed to fetch %s after %d attempts: %s", url, max_retries, exc)
    return None


def fetch_google_news_rss(query: str, max_entries: int = 10) -> List[str]:
    """Fetch Google News RSS feed with AI search query."""
    try:
        # URL encode the query
        encoded_query = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en&gl=US&ceid=US:en"
        
        response = fetch_with_retry(url)
        if not response:
            return []
        
        root = ET.fromstring(response.content)
        links = []
        
        for item in root.findall(".//item")[:max_entries]:
            link_element = item.find("link")
            if link_element is not None and link_element.text:
                # Google News links are encoded, extract actual URL
                link_text = link_element.text.strip()
                # Try to extract URL from Google News redirect
                if "url?q=" in link_text:
                    try:
                        actual_url = urllib.parse.parse_qs(urllib.parse.urlparse(link_text).query).get("q", [link_text])[0]
                        links.append(actual_url)
                    except:
                        links.append(link_text)
                else:
                    links.append(link_text)
        
        logging.debug("Found %d links from Google News query: %s", len(links), query[:50])
        return links
    except Exception as exc:
        logging.warning("Failed to fetch Google News RSS for '%s': %s", query, exc)
        return []


def fetch_reddit_posts(subreddit: str, max_posts: int = 10) -> List[str]:
    """Fetch Reddit posts from a subreddit using RSS feed (no auth needed)."""
    try:
        # Use RSS feed instead of JSON API to avoid 403 blocks
        url = f"https://www.reddit.com/r/{subreddit}/.rss"
        headers = get_headers()
        headers["User-Agent"] = "TechNewsDailyBot/1.0 (Contact: github.com/yourusername)"  # Reddit-friendly user agent
        
        response = fetch_with_retry(url, headers=headers, timeout=20)
        if not response:
            return []
        
        # Parse RSS XML
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            logging.warning("Malformed RSS for Reddit r/%s: %s", subreddit, exc)
            return []
        
        links = []
        # Extract links from RSS items
        for item in root.findall(".//item")[:max_posts]:
            link_element = item.find("link")
            if link_element is not None and link_element.text:
                link_url = link_element.text.strip()
                # Only get external links (not Reddit self-posts)
                if not link_url.startswith("https://www.reddit.com"):
                    links.append(link_url)
        
        logging.debug("Found %d links from r/%s", len(links), subreddit)
        return links
    except Exception as exc:
        logging.warning("Failed to fetch Reddit r/%s: %s", subreddit, exc)
        return []


def fetch_hackernews_stories(max_stories: int = 30) -> List[Dict]:
    """Fetch Hacker News top stories and filter for AI-related."""
    try:
        # Get top story IDs
        response = fetch_with_retry("https://hacker-news.firebaseio.com/v0/topstories.json")
        if not response:
            return []
        
        story_ids = response.json()[:max_stories]
        stories = []
        
        for story_id in story_ids:
            try:
                story_response = fetch_with_retry(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json")
                if story_response:
                    story_data = story_response.json()
                    # Only include stories with URLs (not Ask HN, etc.)
                    if story_data.get("url") and story_data.get("title"):
                        stories.append({
                            "url": story_data["url"],
                            "title": story_data["title"],
                            "score": story_data.get("score", 0),
                        })
            except Exception as exc:
                logging.debug("Failed to fetch HN story %d: %s", story_id, exc)
                continue
        
        logging.debug("Found %d Hacker News stories", len(stories))
        return stories
    except Exception as exc:
        logging.warning("Failed to fetch Hacker News stories: %s", exc)
        return []


def fetch_rss_links(source: SourceFeed, max_entries: int = 5) -> List[str]:
    """Fetch links from RSS feed or other source types."""
    logging.info("Fetching %s source: %s", source.source_type, source.name)
    
    # Handle different source types
    if source.source_type == "googlenews":
        if source.search_query:
            return fetch_google_news_rss(source.search_query, max_entries)
        return []
    
    elif source.source_type == "reddit":
        # Extract subreddit from name (format: "Reddit: subredditname" or "Reddit: MachineLearning")
        subreddit = source.name.lower()
        if "reddit:" in subreddit:
            subreddit = subreddit.split("reddit:")[-1].strip()
        else:
            subreddit = subreddit.replace("reddit ", "").replace("r/", "").strip()
        # Clean up any remaining formatting
        subreddit = subreddit.replace(" ", "").replace("/", "")
        logging.debug("Extracted subreddit: %s from source name: %s", subreddit, source.name)
        return fetch_reddit_posts(subreddit, max_entries)
    
    elif source.source_type == "hackernews":
        stories = fetch_hackernews_stories(max_entries * 3)  # Get more to filter
        # Filter for AI-related stories by title
        ai_stories = []
        for story in stories:
            title_lower = story["title"].lower()
            # Quick AI keyword check
            if any(kw in title_lower for kw in ["ai", "artificial intelligence", "machine learning", "llm", "gpt", "claude", "gemini", "neural"]):
                ai_stories.append(story["url"])
                if len(ai_stories) >= max_entries:
                    break
        return ai_stories
    
    elif source.source_type == "rss" and source.rss_url:
        # Standard RSS feed
        headers = get_headers(source.headers) if source.headers else get_headers()
        response = fetch_with_retry(source.rss_url, headers=headers)
        if not response:
            return []

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            logging.warning("Malformed RSS for %s: %s", source.name, exc)
            return []

        links: List[str] = []
        for item in root.findall(".//item")[:max_entries]:
            link_element = item.find("link")
            if link_element is not None and link_element.text:
                links.append(link_element.text.strip())
        logging.debug("Found %d links for %s", len(links), source.name)
        return links
    
    return []


def should_exclude_article(candidate: ArticleCandidate) -> Optional[str]:
    """Check if article should be excluded based on low-value content keywords.
    Returns exclusion reason if article should be excluded, None otherwise."""
    text = f"{candidate.title} {candidate.summary}".lower()
    
    for keyword in EXCLUSION_KEYWORDS:
        if keyword.lower() in text:
            return f"contains '{keyword}'"
    
    return None


def has_ai_in_primary_context(candidate: ArticleCandidate) -> bool:
    """Check if AI keywords appear in the primary context (title or first 200 chars of summary).
    This ensures AI is the main topic, not just mentioned in passing."""
    primary_text = f"{candidate.title} {candidate.summary[:200]}".lower()
    
    # Check for high-weight AI keywords in primary context
    high_weight_found = False
    for keyword, weight in AI_KEYWORDS.items():
        if weight >= 2.5 and keyword.lower() in primary_text:
            high_weight_found = True
            break
    
    # Also check for any AI keywords (even lower weight) in title
    title_lower = candidate.title.lower()
    title_ai_found = False
    for keyword in AI_KEYWORDS.keys():
        if keyword.lower() in title_lower:
            title_ai_found = True
            break
    
    return high_weight_found or title_ai_found


def calculate_ai_density(candidate: ArticleCandidate) -> float:
    """Calculate AI keyword density in title + summary (first 300 words).
    Returns percentage of words that are AI-related keywords."""
    primary_text = f"{candidate.title} {candidate.summary[:2000]}".lower()
    words = primary_text.split()
    
    if not words:
        return 0.0
    
    ai_keyword_count = 0
    for keyword in AI_KEYWORDS.keys():
        # Count occurrences of keyword in text
        ai_keyword_count += primary_text.count(keyword.lower())
    
    # Calculate density as percentage
    density = (ai_keyword_count / len(words)) * 100
    return round(density, 2)


def is_major_ai_news(candidate: ArticleCandidate, config: Config) -> bool:
    """Check if article represents major AI news (has AI keywords AND major news indicators)."""
    if not config.ai_only_mode:
        return True
    
    text = f"{candidate.title} {candidate.summary}".lower()
    
    # Must have AI keywords
    has_ai_keywords = False
    for keyword in AI_KEYWORDS.keys():
        if keyword.lower() in text:
            has_ai_keywords = True
            break
    
    if not has_ai_keywords:
        return False
    
    # Must have major news indicators
    has_major_news = False
    for indicator in MAJOR_NEWS_INDICATORS.keys():
        if indicator.lower() in text:
            has_major_news = True
            break
    
    # Check content depth
    word_count = len(candidate.text.split())
    has_depth = word_count >= 300  # Substantial content
    
    # Check recency (prefer recent news)
    is_recent = True
    if candidate.published:
        age_hours = (datetime.now(timezone.utc) - candidate.published).total_seconds() / 3600
        is_recent = age_hours <= 72  # Within 3 days
    
    return has_ai_keywords and (has_major_news or (has_depth and is_recent))


def is_ai_related(candidate: ArticleCandidate, config: Config) -> bool:
    """Check if article contains AI-related keywords in meaningful context.
    Requires AI to be in primary context (title/summary) and meet density threshold."""
    if not config.ai_only_mode:
        return True  # If AI-only mode is disabled, accept all articles
    
    # First check: AI must be in primary context (title or summary start)
    if not has_ai_in_primary_context(candidate):
        logging.debug("Article '%s' rejected: AI keywords not in primary context", candidate.title[:50])
        return False
    
    # Second check: Calculate AI keyword density
    density = calculate_ai_density(candidate)
    if density < config.min_ai_density:
        logging.debug("Article '%s' rejected: AI density %.2f%% below threshold %.2f%%", 
                     candidate.title[:50], density, config.min_ai_density)
        return False
    
    # Third check: Count AI keywords in full text (title + summary + first 500 chars of body)
    text = f"{candidate.title} {candidate.summary} {candidate.text[:500]}".lower()
    ai_keyword_count = 0
    high_weight_matches = 0
    
    for keyword, weight in AI_KEYWORDS.items():
        if keyword.lower() in text:
            ai_keyword_count += 1
            # High-weight keywords indicate stronger AI focus
            if weight >= 2.5:
                high_weight_matches += 1
                ai_keyword_count += 1  # Count as double
    
    # Require at least minimum keywords, with preference for high-weight matches
    if high_weight_matches > 0:
        return ai_keyword_count >= config.min_ai_keywords
    else:
        # If no high-weight matches, require more total matches
        return ai_keyword_count >= max(config.min_ai_keywords, 2)


def load_article(url: str, source_name: str, config: Optional[Config] = None) -> Optional[ArticleCandidate]:
    article = Article(url=url, language="en")
    try:
        article.download()
        article.parse()
        article.nlp()
    except Exception as exc:  # pragma: no cover - third-party behavior
        logging.warning("Unable to parse article %s: %s", url, exc)
        return None

    if len(article.text.split()) < 120:
        logging.info("Skipping short article (%s)", url)
        return None

    published = None
    if article.publish_date:
        published = article.publish_date
        if isinstance(published, datetime) and published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

    summary = article.summary or article.text[:400]

    candidate = ArticleCandidate(
        title=article.title or "Untitled",
        url=url,
        summary=summary,
        text=article.text,
        image_url=article.top_image if article.top_image else None,
        published=published,
        source=source_name,
    )
    
    # Check exclusion keywords early - reject immediately if found
    if config:
        exclusion_reason = should_exclude_article(candidate)
        if exclusion_reason:
            logging.info("Excluded article '%s': %s", candidate.title[:60], exclusion_reason)
            return None
        
        # Filter for AI content if AI-only mode is enabled
        if not is_ai_related(candidate, config):
            density = calculate_ai_density(candidate)
            logging.debug("Filtered out non-AI article: '%s' (density: %.2f%%)", 
                         candidate.title[:50], density)
            return None
    
    return candidate


def score_article(candidate: ArticleCandidate, source_weight: float, config: Config) -> float:
    # Check exclusion keywords first - reject immediately if found
    exclusion_reason = should_exclude_article(candidate)
    if exclusion_reason:
        logging.debug("Excluding article '%s': %s", candidate.title[:50], exclusion_reason)
        return 0.0
    
    # CRITICAL: Require AI in primary context for AI-only mode
    if config.ai_only_mode and not has_ai_in_primary_context(candidate):
        logging.debug("Rejecting article '%s': AI not in primary context", candidate.title[:50])
        return 0.0
    
    score = 0.0
    text = f"{candidate.title} {candidate.summary}".lower()
    primary_text = f"{candidate.title} {candidate.summary[:200]}".lower()
    
    # Count AI keyword matches (prioritize primary context)
    ai_keyword_matches = 0
    ai_keyword_score = 0.0
    primary_ai_matches = 0  # AI keywords in primary context
    
    for keyword, weight in AI_KEYWORDS.items():
        keyword_lower = keyword.lower()
        if keyword_lower in text:
            ai_keyword_matches += 1
            # Apply boost multiplier for AI keywords
            ai_keyword_score += weight * config.ai_keyword_boost
            
            # Extra boost if in primary context
            if keyword_lower in primary_text:
                primary_ai_matches += 1
                ai_keyword_score += weight * 0.5  # Additional boost for primary context
    
    # Reject articles with no AI keywords (if AI-only mode is enabled)
    if config.ai_only_mode and ai_keyword_matches == 0:
        return 0.0
    
    score += ai_keyword_score
    
    # Major news indicator bonuses (ONLY if AI is in primary context)
    major_news_score = 0.0
    major_indicators_found = []
    if primary_ai_matches > 0:  # Only apply if AI is in primary context
        for indicator, weight in MAJOR_NEWS_INDICATORS.items():
            if indicator.lower() in text:
                major_news_score += weight
                major_indicators_found.append(indicator)
    
    if major_indicators_found:
        score += major_news_score
        logging.debug("Major news indicators found in '%s': %s", candidate.title[:50], ", ".join(major_indicators_found[:3]))
    
    # Extra bonus for articles with both AI keywords AND major news indicators (in primary context)
    if primary_ai_matches > 0 and major_news_score > 0:
        score += 3.0  # Significant boost for major AI news
    
    # Bonus for multiple AI keyword matches in primary context
    if primary_ai_matches >= 3:
        score += 2.5  # Strong AI focus
    elif primary_ai_matches >= 2:
        score += 1.5
    elif primary_ai_matches >= 1:
        score += 0.5
    
    # Bonus for multiple AI keyword matches overall
    if ai_keyword_matches >= 3:
        score += 2.0  # Multiple AI terms indicate strong AI focus
    elif ai_keyword_matches >= 2:
        score += 1.0
    
    # Recency bonus (stronger for AI news)
    if candidate.published:
        age_hours = (datetime.now(timezone.utc) - candidate.published).total_seconds() / 3600
        freshness_bonus = max(0, 48 - age_hours) / 48  # 0..1
        score += freshness_bonus * 2.5  # Increased from 2.0 for AI news
    
    score += source_weight

    # Content depth bonus
    if len(candidate.text.split()) > 600:
        score += 0.5
    
    # Apply minimum score threshold
    if config.ai_only_mode and score < config.min_ai_score:
        logging.debug("Rejecting article '%s': score %.2f below minimum %.2f", 
                     candidate.title[:50], score, config.min_ai_score)
        return 0.0

    return round(score, 2)


def rank_articles(candidates: List[ArticleCandidate], sources: List[SourceFeed], config: Config) -> List[ArticleCandidate]:
    weights = {source.name: source.weight for source in sources}
    for candidate in candidates:
        candidate.score = score_article(candidate, weights.get(candidate.source, 1.0), config)
    
    # Filter out zero-scored articles (non-AI articles in AI-only mode)
    if config.ai_only_mode:
        candidates = [c for c in candidates if c.score > 0]
    
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def collect_candidates(sources: List[SourceFeed], max_articles: int, config: Config) -> List[ArticleCandidate]:
    seen_links = set()
    candidates: List[ArticleCandidate] = []
    total_articles_checked = 0
    ai_articles_found = 0
    excluded_count = 0
    major_news_count = 0
    sources_succeeded = 0
    sources_failed = 0
    
    # Sort sources by weight (highest first) to prioritize AI-focused sources
    sorted_sources = sorted(sources, key=lambda s: s.weight, reverse=True)
    
    for source in sorted_sources:
        if len(candidates) >= max_articles:
            break
        
        try:
            links = fetch_rss_links(source, max_entries=10)  # Get more links per source
            
            if not links:
                sources_failed += 1
                logging.debug("No links found from %s", source.name)
                continue
            
            sources_succeeded += 1
            logging.debug("Successfully fetched %d links from %s", len(links), source.name)
            
            for link in links:
                if len(candidates) >= max_articles:
                    break
                if link in seen_links:
                    continue
                seen_links.add(link)
                total_articles_checked += 1
                
                try:
                    article = load_article(link, source.name, config)
                    if article:
                        # Check if it's major AI news
                        if is_major_ai_news(article, config):
                            major_news_count += 1
                        candidates.append(article)
                        ai_articles_found += 1
                    else:
                        excluded_count += 1
                except Exception as exc:
                    logging.debug("Failed to load article %s: %s", link[:50], exc)
                    excluded_count += 1
        except Exception as exc:
            sources_failed += 1
            logging.warning("Error fetching from %s: %s", source.name, exc)
            continue
    
    if config.ai_only_mode:
        logging.info("Article collection stats: %d sources succeeded, %d failed | %d articles checked, %d excluded (sales/deals/non-AI), %d AI-related collected (%d major news)", 
                    sources_succeeded, sources_failed, total_articles_checked, excluded_count, ai_articles_found, major_news_count)
    else:
        logging.info("Collected %d candidate articles from %d checked (%d sources succeeded, %d failed)", 
                    len(candidates), total_articles_checked, sources_succeeded, sources_failed)
    
    # Ensure we have at least some articles before proceeding
    if len(candidates) == 0 and sources_succeeded == 0:
        logging.error("All sources failed! Check your internet connection and source availability.")
    
    return candidates


def select_top_story(sources: List[SourceFeed], max_articles: int, config: Config) -> Optional[ArticleCandidate]:
    candidates = collect_candidates(sources, max_articles, config)
    if not candidates:
        if config.ai_only_mode:
            logging.error("No AI-related articles available for selection")
        else:
            logging.error("No articles available for selection")
        return None
    ranked = rank_articles(candidates, sources, config)
    if not ranked:
        if config.ai_only_mode:
            logging.error("No AI-related articles available for selection")
        else:
            logging.error("No articles available for selection")
        return None
    
    top = ranked[0]
    
    # Log detailed information about selected article
    density = calculate_ai_density(top)
    text_lower = f"{top.title} {top.summary}".lower()
    major_indicators = [ind for ind in MAJOR_NEWS_INDICATORS.keys() if ind.lower() in text_lower]
    
    # Find AI keywords in primary context
    primary_text = f"{top.title} {top.summary[:200]}".lower()
    primary_ai_keywords = [kw for kw in AI_KEYWORDS.keys() if kw.lower() in primary_text]
    
    if major_indicators:
        logging.info("Selected major AI story '%s' (score: %.2f, density: %.2f%%) - Indicators: %s, AI keywords: %s", 
                    top.title[:60], top.score, density, ", ".join(major_indicators[:3]), 
                    ", ".join(primary_ai_keywords[:5]))
    else:
        logging.info("Selected AI story '%s' (score: %.2f, density: %.2f%%) - AI keywords: %s", 
                    top.title[:60], top.score, density, ", ".join(primary_ai_keywords[:5]))
    
    return top


def extract_key_points(text: str, max_points: int) -> List[str]:
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if sentence.strip()
    ]
    return sentences[:max_points]


def truncate_script_to_word_limit(script: str, max_words: int) -> str:
    """Truncate script to maximum word count, ensuring it ends at a sentence boundary."""
    if not script:
        return ""
    
    words = script.split()
    if len(words) <= max_words:
        return script
    
    # Truncate to max_words
    truncated_words = words[:max_words]
    truncated_text = " ".join(truncated_words)
    
    # Try to end at a sentence boundary
    # Look for the last sentence-ending punctuation
    last_period = truncated_text.rfind(".")
    last_exclamation = truncated_text.rfind("!")
    last_question = truncated_text.rfind("?")
    
    last_sentence_end = max(last_period, last_exclamation, last_question)
    
    if last_sentence_end > len(truncated_text) * 0.7:  # If sentence end is in last 30% of text
        truncated_text = truncated_text[:last_sentence_end + 1]
    else:
        # No good sentence boundary, just add ellipsis
        truncated_text = truncated_text.rstrip(".,!?") + "..."
    
    return truncated_text


def clean_script_for_tts(script: str) -> str:
    """Clean and parse script text for TTS, removing markdown, response prefixes, and formatting."""
    if not script:
        return ""
    
    # Remove markdown code blocks
    script = re.sub(r"```[\w]*\n?", "", script)
    script = re.sub(r"```", "", script)
    
    # Remove markdown formatting
    script = re.sub(r"\*\*([^*]+)\*\*", r"\1", script)  # Bold
    script = re.sub(r"\*([^*]+)\*", r"\1", script)  # Italic
    script = re.sub(r"__([^_]+)__", r"\1", script)  # Bold (underscore)
    script = re.sub(r"_([^_]+)_", r"\1", script)  # Italic (underscore)
    script = re.sub(r"#+\s*", "", script)  # Headers
    script = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", script)  # Links [text](url) -> text
    script = re.sub(r"`([^`]+)`", r"\1", script)  # Inline code
    
    # Remove common response prefixes (case-insensitive)
    prefixes = [
        r"^here's the script:?\s*",
        r"^script:?\s*",
        r"^here is the script:?\s*",
        r"^the script:?\s*",
        r"^script for the video:?\s*",
        r"^video script:?\s*",
        r"^narration:?\s*",
        r"^voiceover:?\s*",
    ]
    for prefix in prefixes:
        script = re.sub(prefix, "", script, flags=re.IGNORECASE)
    
    # Remove lines that are just formatting or metadata
    lines = script.split("\n")
    cleaned_lines = []
    for line in lines:
        line = line.strip()
        # Skip empty lines, markdown list markers, and metadata lines
        if not line:
            continue
        if re.match(r"^[-*+]\s+", line):  # List markers
            line = re.sub(r"^[-*+]\s+", "", line)
        if re.match(r"^\d+\.\s+", line):  # Numbered list
            line = re.sub(r"^\d+\.\s+", "", line)
        # Skip lines that look like metadata or instructions
        if any(skip in line.lower() for skip in ["requirements:", "note:", "instructions:", "duration:", "target:"]):
            continue
        cleaned_lines.append(line)
    
    # Join lines and clean up whitespace
    script = " ".join(cleaned_lines)
    
    # Remove extra whitespace
    script = re.sub(r"\s+", " ", script)
    script = script.strip()
    
    # Remove trailing punctuation issues
    script = re.sub(r"\s+([.!?])+", r"\1", script)  # Multiple punctuation -> single
    
    return script


def generate_script_with_gemini(article: ArticleCandidate, config: Config, max_retries: int = 3) -> Optional[str]:
    """Generate script using Google Gemini API with retry logic."""
    if not config.use_gemini or not config.gemini_api_key:
        return None
    
    if genai is None:
        logging.warning("google-generativeai not available")
        return None
    
    genai.configure(api_key=config.gemini_api_key)
    model = genai.GenerativeModel(config.gemini_model)
    
    # Calculate target word count (2.5 words per second for natural speech)
    target_words = min(config.max_script_words, 150)  # Cap at 150 words for 60 seconds
    
    prompt = textwrap.dedent(f"""
    Create an engaging 45-60 second script for a vertical video about this AI news story.
    
    Story Title: {article.title}
    Source: {article.source}
    Summary: {article.summary[:500]}
    
    Requirements:
    - Start with a compelling hook that grabs attention (15-20 words)
    - Include 2-3 key points about the story (60-80 words total)
    - Explain why this AI development matters (20-30 words)
    - End with a call-to-action to follow for daily AI news (5-10 words)
    - Keep the tone energetic and engaging
    - STRICT WORD LIMIT: Maximum {target_words} words total (approximately 60 seconds when spoken)
    - Focus on the AI/ML aspects of the story
    - Be concise and impactful - every word counts
    
    IMPORTANT: 
    - Return ONLY the script text itself, without any markdown formatting, prefixes, or explanations.
    - Do NOT exceed {target_words} words. If you exceed this limit, the script will be cut off.
    - Write the script as natural, conversational text that flows well when spoken.
    - Do not include phrases like "Here's the script:" or "Script:" - just return the script text directly.
    """).strip()
    
    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt)
            raw_script = response.text.strip()
            
            if raw_script:
                # Clean the script to remove markdown and response prefixes
                script = clean_script_for_tts(raw_script)
                
                if not script:
                    logging.warning("Gemini API returned script but cleaning resulted in empty text")
                    return None
                
                # Enforce word limit
                word_count = len(script.split())
                if word_count > config.max_script_words:
                    logging.warning("Script too long (%d words), truncating to %d words", word_count, config.max_script_words)
                    script = truncate_script_to_word_limit(script, config.max_script_words)
                    word_count = len(script.split())
                
                # Log token usage if available
                if hasattr(response, 'usage_metadata'):
                    usage = response.usage_metadata
                    logging.info("Gemini API usage: %d prompt tokens, %d completion tokens", 
                               usage.prompt_token_count if hasattr(usage, 'prompt_token_count') else 0,
                               usage.candidates_token_count if hasattr(usage, 'candidates_token_count') else 0)
                
                logging.info("Generated and cleaned script using Gemini API (%d words)", word_count)
                return script
            else:
                logging.warning("Gemini API returned empty script")
                return None
                
        except Exception as exc:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                logging.warning("Gemini API attempt %d/%d failed: %s, retrying in %ds", 
                              attempt + 1, max_retries, exc, wait_time)
                time.sleep(wait_time)
            else:
                logging.warning("Gemini API script generation failed after %d attempts: %s", max_retries, exc)
                return None
    
    return None


def generate_script(article: ArticleCandidate, config: Config) -> str:
    """Generate script with Gemini API fallback to template."""
    # Try Gemini API first
    if config.use_gemini:
        gemini_script = generate_script_with_gemini(article, config)
        if gemini_script:
            # Script is already cleaned, just format for display
            return textwrap.fill(gemini_script, width=90)
        logging.info("Falling back to template-based script generation")
    
    # Fallback to template-based script
    title_lower = article.title.lower()
    if any(ai_term in title_lower for ai_term in ["ai", "artificial intelligence", "machine learning", "gpt", "claude"]):
        hook = f"Breaking AI news: {article.title}."
    else:
        hook = f"Breaking AI news from {article.source}: {article.title}."
    
    key_points = extract_key_points(article.summary or article.text, config.max_script_points)
    if not key_points:
        key_points = ["This AI development is making waves in the tech world."]

    points_text = " ".join(key_points)
    outro = "Follow for daily AI news updates."
    script = " ".join([hook, points_text, outro])
    
    # Clean template script too (in case it has any formatting)
    script = clean_script_for_tts(script)
    
    # Enforce word limit for template scripts too
    word_count = len(script.split())
    if word_count > config.max_script_words:
        logging.warning("Template script too long (%d words), truncating to %d words", word_count, config.max_script_words)
        script = truncate_script_to_word_limit(script, config.max_script_words)
        word_count = len(script.split())
    
    logging.info("Generated AI-focused script using template summarizer (%d words)", word_count)
    return textwrap.fill(script, width=90)


def generate_metadata(article: ArticleCandidate, script: str) -> Dict[str, str]:
    # AI-focused hashtags
    primary_hashtags = ["#AI", "#ArtificialIntelligence", "#MachineLearning"]
    secondary_hashtags = ["#TechAI", "#AITools", "#AIBreakthrough", "#GenerativeAI", "#AITech"]
    hashtags = primary_hashtags + secondary_hashtags[:3]  # Use primary + 3 secondary
    
    description = f"{article.summary}\nRead more: {article.url}\n" + " ".join(hashtags)
    
    # Extract AI-related tags from article
    text_lower = article.title.lower() + " " + article.summary.lower()
    ai_tags = []
    for keyword in AI_KEYWORDS.keys():
        if keyword.lower() in text_lower and keyword not in ai_tags:
            # Use simplified tag versions
            tag = keyword.replace(" ", "").replace("-", "").lower()
            if len(tag) <= 20:  # Keep tags reasonable length
                ai_tags.append(tag)
    
    # Base tags with AI focus
    tags = ["ai", "artificialintelligence", "machinelearning", "tech", "news", article.source.lower()] + ai_tags[:5]
    
    # Title with AI context
    title = article.title
    if not any(ai_term in title.lower() for ai_term in ["ai", "artificial intelligence", "machine learning"]):
        title = f"AI News: {title}"
    title = f"{title} — Explained in 60s"
    
    metadata = {
        "title": title,
        "description": description,
        "tags": ",".join(dict.fromkeys(tags))[:400],
    }
    logging.debug("Metadata: %s", metadata)
    return metadata


def find_youtube_channel(youtube, channel_name: str) -> Optional[str]:
    """Find YouTube channel ID by channel name or handle.
    
    Args:
        youtube: Authenticated YouTube API service object
        channel_name: Channel name (e.g., "Code Rush") or handle (e.g., "@CodeRush_AI")
        
    Returns:
        Channel ID if found, None otherwise
    """
    try:
        # List all channels accessible by the authenticated user
        channels_response = youtube.channels().list(
            part="snippet,id",
            mine=True,
            maxResults=50
        ).execute()
        
        channels = channels_response.get("items", [])
        
        if not channels:
            logging.warning("No channels found for authenticated account")
            return None
        
        # Normalize search terms
        search_name = channel_name.lower().strip()
        # Remove @ symbol if present
        if search_name.startswith("@"):
            search_name = search_name[1:]
        
        # Search for matching channel
        for channel in channels:
            channel_id = channel["id"]
            snippet = channel.get("snippet", {})
            title = snippet.get("title", "").lower()
            custom_url = snippet.get("customUrl", "").lower()
            # Remove @ from custom URL for comparison
            if custom_url.startswith("@"):
                custom_url = custom_url[1:]
            
            # Check if channel name or handle matches
            if (search_name in title or 
                search_name == custom_url or
                search_name in custom_url or
                custom_url in search_name):
                channel_title = snippet.get("title", "Unknown")
                channel_handle = snippet.get("customUrl", "")
                logging.info("Found matching channel: '%s' (%s) - ID: %s", 
                           channel_title, channel_handle or "no handle", channel_id)
                return channel_id
        
        # If no exact match, log available channels for debugging
        logging.warning("Channel '%s' not found. Available channels:", channel_name)
        for channel in channels:
            snippet = channel.get("snippet", {})
            title = snippet.get("title", "Unknown")
            handle = snippet.get("customUrl", "")
            logging.warning("  - '%s' (%s)", title, handle or "no handle")
        
        # Return the first channel as fallback (default channel)
        if channels:
            default_channel = channels[0]
            default_title = default_channel.get("snippet", {}).get("title", "Unknown")
            default_id = default_channel["id"]
            logging.warning("Using default channel '%s' (ID: %s) as fallback", default_title, default_id)
            return default_id
        
        return None
        
    except Exception as exc:
        logging.error("Error finding YouTube channel: %s", exc)
        return None


def upload_to_youtube(video_path: Path, title: str, description: str, tags: str, config: Config, max_retries: int = 3) -> Optional[str]:
    """Upload video to YouTube using OAuth 2.0 and YouTube Data API v3.
    
    Args:
        video_path: Path to the video file to upload
        title: Video title
        description: Video description
        tags: Comma-separated tags
        config: Config object with YouTube credentials
        max_retries: Maximum number of retry attempts
        
    Returns:
        Video ID if successful, None otherwise
    """
    if not config.upload_to_youtube:
        logging.info("YouTube upload disabled in config")
        return None
    
    if not all([config.youtube_client_id, config.youtube_client_secret, config.youtube_refresh_token]):
        missing = []
        if not config.youtube_client_id: missing.append("YT_CLIENT_ID")
        if not config.youtube_client_secret: missing.append("YT_CLIENT_SECRET")
        if not config.youtube_refresh_token: missing.append("YT_REFRESH_TOKEN")
        logging.warning("YouTube credentials not configured, missing: %s", ", ".join(missing))
        return None
    
    if not video_path.exists():
        logging.error("Video file not found: %s", video_path)
        return None
    
    if build is None or Credentials is None or MediaFileUpload is None:
        logging.warning("YouTube API libraries not available, skipping upload")
        return None
    
    # Check file size (YouTube accepts up to 256GB, but we should warn if very large)
    file_size_mb = video_path.stat().st_size / (1024 * 1024)
    if file_size_mb > 100:
        logging.warning("Video file is large (%.2f MB), upload may take a while", file_size_mb)
    
    for attempt in range(max_retries):
        try:
            # Create credentials from refresh token
            creds = Credentials(
                token=None,
                refresh_token=config.youtube_refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=config.youtube_client_id,
                client_secret=config.youtube_client_secret,
            )
            
            # Refresh the access token
            request = Request()
            creds.refresh(request)
            
            # Build YouTube service
            youtube = build("youtube", "v3", credentials=creds)
            
            # Find and verify the target channel
            target_channel_id = None
            if config.youtube_channel_name:
                logging.info("Looking for YouTube channel: '%s'", config.youtube_channel_name)
                target_channel_id = find_youtube_channel(youtube, config.youtube_channel_name)
                if target_channel_id:
                    logging.info("Will upload to channel ID: %s", target_channel_id)
                else:
                    logging.warning("Could not find specified channel '%s', will use default channel", 
                                  config.youtube_channel_name)
            else:
                # Get default channel
                try:
                    channels_response = youtube.channels().list(
                        part="snippet,id",
                        mine=True,
                        maxResults=1
                    ).execute()
                    if channels_response.get("items"):
                        target_channel_id = channels_response["items"][0]["id"]
                        channel_title = channels_response["items"][0].get("snippet", {}).get("title", "Unknown")
                        logging.info("Using default channel: '%s' (ID: %s)", channel_title, target_channel_id)
                except Exception as exc:
                    logging.warning("Could not determine channel, proceeding with upload: %s", exc)
            
            # Prepare video metadata
            body = {
                "snippet": {
                    "title": title[:100],  # YouTube title limit is 100 characters
                    "description": description[:5000],  # YouTube description limit is 5000 characters
                    "tags": tags.split(",")[:500] if tags else [],  # YouTube allows up to 500 tags
                    "categoryId": "22",  # People & Blogs category
                },
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                },
            }
            
            # Prepare media file upload with resumable upload
            media = MediaFileUpload(
                str(video_path),
                chunksize=-1,  # Use default chunk size for resumable uploads
                resumable=True,
            )
            
            # Insert video
            logging.info("Uploading video to YouTube (attempt %d/%d)...", attempt + 1, max_retries)
            insert_request = youtube.videos().insert(
                part=",".join(body.keys()),
                body=body,
                media_body=media,
            )
            
            # Execute resumable upload
            response = None
            chunk_count = 0
            while response is None:
                status, response = insert_request.next_chunk()
                if status:
                    chunk_count += 1
                    # Try to get progress percentage if available
                    try:
                        if hasattr(status, 'resumable_progress'):
                            progress_obj = status.resumable_progress
                            if hasattr(progress_obj, 'bytes_uploaded') and hasattr(progress_obj, 'total_bytes_uploaded'):
                                uploaded = progress_obj.bytes_uploaded
                                total = progress_obj.total_bytes_uploaded
                                if total > 0:
                                    progress = int((uploaded / total) * 100)
                                    logging.info("Upload progress: %d%% (%d/%d bytes)", progress, uploaded, total)
                        else:
                            logging.debug("Upload in progress (chunk %d)...", chunk_count)
                    except Exception:
                        logging.debug("Upload in progress (chunk %d)...", chunk_count)
            
            if "id" in response:
                video_id = response["id"]
                logging.info("Successfully uploaded video to YouTube: https://www.youtube.com/watch?v=%s", video_id)
                return video_id
            else:
                logging.error("YouTube upload completed but no video ID returned")
                return None
                
        except HttpError as exc:
            error_details = exc.error_details if hasattr(exc, 'error_details') else str(exc)
            if exc.resp.status == 401:
                logging.error("YouTube authentication failed. Refresh token may be expired. "
                            "Please regenerate the token using youtube_oauth.py script.")
                return None
            elif exc.resp.status == 403:
                logging.error("YouTube API quota exceeded or permission denied: %s", error_details)
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logging.info("Retrying in %d seconds...", wait_time)
                    time.sleep(wait_time)
                else:
                    return None
            else:
                logging.error("YouTube API error (attempt %d/%d): %s", attempt + 1, max_retries, error_details)
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logging.info("Retrying in %d seconds...", wait_time)
                    time.sleep(wait_time)
                else:
                    return None
                    
        except Exception as exc:
            logging.error("YouTube upload error (attempt %d/%d): %s", attempt + 1, max_retries, exc)
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logging.info("Retrying in %d seconds...", wait_time)
                time.sleep(wait_time)
            else:
                return None
    
    return None


def upload_to_tiktok(video_path: Path, title: str, config: Config, max_retries: int = 3) -> Optional[str]:
    """Upload video to TikTok using Content Posting API v2.
    
    Args:
        video_path: Path to the video file to upload
        title: Video title/caption
        config: Config object with TikTok credentials
        max_retries: Maximum number of retry attempts
        
    Returns:
        Video ID if successful, None otherwise
    """
    if not config.upload_to_tiktok:
        logging.info("TikTok upload disabled in config")
        return None
    
    if not all([config.tiktok_client_key, config.tiktok_client_secret, config.tiktok_access_token]):
        missing = []
        if not config.tiktok_client_key: missing.append("TIKTOK_CLIENT_KEY")
        if not config.tiktok_client_secret: missing.append("TIKTOK_CLIENT_SECRET")
        if not config.tiktok_access_token: missing.append("TIKTOK_ACCESS_TOKEN")
        logging.warning("TikTok credentials not configured, missing: %s", ", ".join(missing))
        return None
    
    if not video_path.exists():
        logging.error("Video file not found: %s", video_path)
        return None
    
    # Check file size (TikTok limit is 50MB)
    file_size_mb = video_path.stat().st_size / (1024 * 1024)
    if file_size_mb > 50:
        logging.error("Video file size (%.2f MB) exceeds TikTok limit (50 MB)", file_size_mb)
        return None
    
    # TikTok API base URL
    base_url = "https://open.tiktokapis.com/v2/"
    headers = {
        "Authorization": f"Bearer {config.tiktok_access_token}",
    }
    
    for attempt in range(max_retries):
        try:
            # Step 1: Initialize upload to get upload_url
            logging.info("Initializing TikTok upload (attempt %d/%d)...", attempt + 1, max_retries)
            
            init_payload = {
                "post_info": {
                    "title": title[:150],  # TikTok title limit is 150 characters
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "video_cover_timestamp_ms": 1000,  # Use 1 second as cover
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                },
            }
            
            init_response = requests.post(
                f"{base_url}post/publish/video/init/",
                headers=headers,
                json=init_payload,
                timeout=30,
            )
            init_response.raise_for_status()
            init_data = init_response.json()
            
            if init_data.get("error"):
                error_msg = init_data["error"].get("message", "Unknown error")
                logging.error("TikTok upload initialization failed: %s", error_msg)
                return None
            
            publish_id = init_data.get("data", {}).get("publish_id")
            upload_url = init_data.get("data", {}).get("upload_url")
            
            if not publish_id or not upload_url:
                logging.error("TikTok upload initialization failed: missing publish_id or upload_url")
                return None
            
            logging.info("TikTok upload initialized, publish_id: %s", publish_id)
            
            # Step 2: Upload video file (chunked if >20MB)
            file_size = video_path.stat().st_size
            chunk_size = 20 * 1024 * 1024  # 20MB chunks (TikTok recommendation)
            
            if file_size > chunk_size:
                # Chunked upload
                logging.info("Uploading video in chunks (file size: %.2f MB)...", file_size_mb)
                with open(video_path, "rb") as f:
                    chunk_num = 0
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        
                        chunk_headers = {
                            "Content-Type": "video/mp4",
                            "Content-Range": f"bytes {chunk_num * chunk_size}-{chunk_num * chunk_size + len(chunk) - 1}/{file_size}",
                        }
                        
                        chunk_response = requests.put(
                            upload_url,
                            headers=chunk_headers,
                            data=chunk,
                            timeout=60,
                        )
                        chunk_response.raise_for_status()
                        chunk_num += 1
                        logging.debug("Uploaded chunk %d", chunk_num)
            else:
                # Single upload
                logging.info("Uploading video file (%.2f MB)...", file_size_mb)
                with open(video_path, "rb") as f:
                    upload_headers = {
                        "Content-Type": "video/mp4",
                    }
                    upload_response = requests.put(
                        upload_url,
                        headers=upload_headers,
                        data=f.read(),
                        timeout=60,
                    )
                    upload_response.raise_for_status()
            
            logging.info("Video file uploaded successfully")
            
            # Step 3: Poll upload status until published
            max_poll_attempts = 60  # Poll for up to 3 minutes (60 * 3 seconds)
            poll_interval = 3  # Poll every 3 seconds
            
            for poll_attempt in range(max_poll_attempts):
                status_response = requests.post(
                    f"{base_url}post/publish/status/fetch/",
                    headers=headers,
                    json={"publish_id": publish_id},
                    timeout=30,
                )
                status_response.raise_for_status()
                status_data = status_response.json()
                
                if status_data.get("error"):
                    error_msg = status_data["error"].get("message", "Unknown error")
                    logging.error("TikTok status check failed: %s", error_msg)
                    return None
                
                status_code = status_data.get("data", {}).get("status")
                
                if status_code == "PUBLISHED":
                    video_id = status_data.get("data", {}).get("publish_id")
                    logging.info("Successfully published video to TikTok: %s", video_id)
                    return video_id
                elif status_code == "FAILED":
                    failure_reason = status_data.get("data", {}).get("fail_reason", "Unknown reason")
                    logging.error("TikTok upload failed: %s", failure_reason)
                    return None
                elif status_code in ["PROCESSING", "PUBLISHING"]:
                    logging.debug("TikTok upload status: %s (polling...)", status_code)
                    time.sleep(poll_interval)
                else:
                    logging.warning("Unknown TikTok status: %s", status_code)
                    time.sleep(poll_interval)
            
            logging.error("TikTok upload timed out after %d polling attempts", max_poll_attempts)
            return None
            
        except requests.HTTPError as exc:
            if exc.response.status_code == 401:
                logging.error("TikTok authentication failed. Access token may be expired or invalid.")
                return None
            elif exc.response.status_code == 429:
                logging.error("TikTok rate limit exceeded")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt * 10  # Longer wait for rate limits
                    logging.info("Retrying in %d seconds...", wait_time)
                    time.sleep(wait_time)
                else:
                    return None
            else:
                error_msg = "Unknown error"
                try:
                    error_data = exc.response.json()
                    error_msg = error_data.get("error", {}).get("message", str(exc))
                except:
                    error_msg = str(exc)
                logging.error("TikTok API error (attempt %d/%d): %s", attempt + 1, max_retries, error_msg)
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logging.info("Retrying in %d seconds...", wait_time)
                    time.sleep(wait_time)
                else:
                    return None
                    
        except Exception as exc:
            logging.error("TikTok upload error (attempt %d/%d): %s", attempt + 1, max_retries, exc)
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logging.info("Retrying in %d seconds...", wait_time)
                time.sleep(wait_time)
            else:
                return None
    
    return None


def extract_keywords_for_search(article: ArticleCandidate) -> List[str]:
    """Extract AI-focused searchable keywords from article for stock media search."""
    text = f"{article.title} {article.summary}".lower()
    keywords = []
    
    # Prioritize AI-related keywords
    ai_keywords_priority = [
        "artificial intelligence", "ai", "machine learning", "deep learning",
        "neural network", "generative ai", "chatbot", "llm", "gpt", "claude"
    ]
    
    for kw in ai_keywords_priority:
        if kw in text:
            keywords.append(kw)
            if len(keywords) >= 2:  # Get 2 AI keywords if available
                break
    
    # Extract AI company names
    ai_companies = ["openai", "anthropic", "google ai", "microsoft ai", "meta ai", "deepmind"]
    for company in ai_companies:
        if company in text:
            keywords.append(company.replace(" ai", "").replace("ai", "").strip() or "ai")
            break
    
    # Fallback to AI-focused defaults if no keywords found
    if not keywords:
        keywords = ["artificial intelligence", "ai technology", "machine learning"]
    elif len(keywords) < 2:
        # Add AI context to single keyword
        if keywords[0] not in ["ai", "artificial intelligence"]:
            keywords.insert(0, "ai")
    
    # Use first 2-3 keywords, prioritizing AI terms
    return keywords[:3]


def fetch_stock_video(keywords: List[str], config: Config) -> Optional[str]:
    """Fetch stock video from Pexels or Pixabay APIs."""
    search_query = " ".join(keywords[:2]) if keywords else "technology"
    
    # Try Pexels videos first
    if config.pexels_api_key:
        try:
            url = "https://api.pexels.com/videos/search"
            headers = {"Authorization": config.pexels_api_key}
            params = {"query": search_query, "per_page": 1, "orientation": "portrait"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                if data.get("videos") and len(data["videos"]) > 0:
                    video = data["videos"][0]
                    # Get the best quality video file
                    video_files = video.get("video_files", [])
                    if video_files:
                        # Prefer HD quality, fallback to any available
                        hd_video = next((vf for vf in video_files if vf.get("quality") == "hd"), None)
                        video_url = (hd_video or video_files[0]).get("link")
                        if video_url:
                            logging.info("Fetched stock video from Pexels: %s", search_query)
                            return video_url
        except requests.RequestException as exc:
            logging.debug("Pexels video API request failed: %s", exc)
        except Exception as exc:
            logging.debug("Pexels video API error: %s", exc)
    
    # Pixabay doesn't have a video API in the same way, skip for now
    logging.debug("No stock video available")
    return None


def fetch_stock_media(keywords: List[str], config: Config, media_type: str = "photo", count: int = 1) -> List[str]:
    """Fetch stock media (images or videos) from Pexels, Pixabay, or Unsplash APIs.
    Returns a list of URLs."""
    search_query = " ".join(keywords[:2]) if keywords else "technology"
    results = []
    
    if media_type == "video":
        # Try to fetch videos
        video_url = fetch_stock_video(keywords, config)
        if video_url:
            return [video_url]
        return []
    
    # Fetch images
    # Try Pexels first
    if config.pexels_api_key:
        try:
            url = "https://api.pexels.com/v1/search"
            headers = {"Authorization": config.pexels_api_key}
            params = {"query": search_query, "per_page": min(count, 15), "orientation": "portrait"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                photos = data.get("photos", [])
                for photo in photos[:count]:
                    image_url = photo.get("src", {}).get("large") or photo.get("src", {}).get("original")
                    if image_url:
                        results.append(image_url)
                if results:
                    logging.info("Fetched %d stock image(s) from Pexels: %s", len(results), search_query)
                    return results[:count]
        except requests.RequestException as exc:
            logging.debug("Pexels API request failed: %s", exc)
        except Exception as exc:
            logging.debug("Pexels API error: %s", exc)
    
    # Try Pixabay
    if config.pixabay_api_key and len(results) < count:
        try:
            url = "https://pixabay.com/api/"
            params = {
                "key": config.pixabay_api_key,
                "q": search_query,
                "image_type": "photo",
                "orientation": "vertical",
                "per_page": min(count * 2, 20),
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                hits = data.get("hits", [])
                for hit in hits:
                    if len(results) >= count:
                        break
                    image_url = hit.get("largeImageURL") or hit.get("webformatURL")
                    if image_url and image_url not in results:
                        results.append(image_url)
                if results:
                    logging.info("Fetched %d stock image(s) from Pixabay: %s", len(results), search_query)
                    return results[:count]
        except requests.RequestException as exc:
            logging.debug("Pixabay API request failed: %s", exc)
        except Exception as exc:
            logging.debug("Pixabay API error: %s", exc)
    
    # Try Unsplash
    if config.unsplash_api_key and len(results) < count:
        try:
            url = "https://api.unsplash.com/search/photos"
            headers = {"Authorization": f"Client-ID {config.unsplash_api_key}"}
            params = {"query": search_query, "per_page": min(count, 10), "orientation": "portrait"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                photo_results = data.get("results", [])
                for result in photo_results:
                    if len(results) >= count:
                        break
                    image_url = result.get("urls", {}).get("regular") or result.get("urls", {}).get("full")
                    if image_url and image_url not in results:
                        results.append(image_url)
                if results:
                    logging.info("Fetched %d stock image(s) from Unsplash: %s", len(results), search_query)
                    return results[:count]
        except requests.RequestException as exc:
            logging.debug("Unsplash API request failed: %s", exc)
        except Exception as exc:
            logging.debug("Unsplash API error: %s", exc)
    
    if not results:
        logging.debug("No stock media available from any API")
    return results[:count]


def prepare_stock_media(article: ArticleCandidate, config: Config, tmp_path: Path, count: int = 5) -> Tuple[Optional[str], List[Path]]:
    """Prepare stock media (videos and images) for video assembly.
    Returns: (video_path, list_of_image_paths)"""
    keywords = extract_keywords_for_search(article)
    video_path = None
    image_paths = []
    
    # Try to fetch stock video first (most engaging)
    if config.pexels_api_key:
        video_url = fetch_stock_video(keywords, config)
        if video_url:
            try:
                video_file = tmp_path / "stock_video.mp4"
                response = requests.get(video_url, timeout=30, stream=True)
                response.raise_for_status()
                
                # Download with size verification
                total_size = 0
                expected_size = int(response.headers.get('content-length', 0))
                
                with open(video_file, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            total_size += len(chunk)
                
                # Verify download completed
                if expected_size > 0 and total_size < expected_size:
                    logging.warning("Video download incomplete: %d/%d bytes", total_size, expected_size)
                    return None, image_paths
                
                # Verify file is not empty and has reasonable size
                if video_file.stat().st_size < 1000:  # Less than 1KB is suspicious
                    logging.warning("Downloaded video file is too small, likely corrupted")
                    return None, image_paths
                
                video_path = str(video_file)
                logging.info("Downloaded stock video from Pexels (%d KB)", video_file.stat().st_size // 1024)
                return video_path, image_paths
            except Exception as exc:
                logging.warning("Failed to download stock video: %s, falling back to images", exc)
    
    # Fetch multiple stock images
    stock_image_urls = fetch_stock_media(keywords, config, media_type="photo", count=count)
    target_width, target_height = 1080, 1920
    
    for i, image_url in enumerate(stock_image_urls):
        try:
            response = requests.get(image_url, timeout=15)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            img = img.convert("RGB")
            
            # Resize to cover 1080x1920
            img_ratio = img.width / img.height
            target_ratio = target_width / target_height
            
            if img_ratio > target_ratio:
                img = img.resize((int(target_height * img_ratio), target_height), Image.Resampling.LANCZOS)
                left = (img.width - target_width) // 2
                img = img.crop((left, 0, left + target_width, target_height))
            else:
                img = img.resize((target_width, int(target_width / img_ratio)), Image.Resampling.LANCZOS)
                top = (img.height - target_height) // 2
                img = img.crop((0, top, target_width, top + target_height))
            
            image_file = tmp_path / f"stock_image_{i}.jpg"
            img.save(image_file, "JPEG", quality=90)
            image_paths.append(image_file)
        except Exception as exc:
            logging.debug("Failed to download/process stock image %d: %s", i, exc)
            continue
    
    if image_paths:
        logging.info("Prepared %d stock image(s) for video", len(image_paths))
        return None, image_paths
    
    # Fallback to article image if available
    if article.image_url:
        try:
            response = requests.get(article.image_url, timeout=10)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            img = img.convert("RGB")
            
            img_ratio = img.width / img.height
            target_ratio = target_width / target_height
            
            if img_ratio > target_ratio:
                img = img.resize((int(target_height * img_ratio), target_height), Image.Resampling.LANCZOS)
                left = (img.width - target_width) // 2
                img = img.crop((left, 0, left + target_width, target_height))
            else:
                img = img.resize((target_width, int(target_width / img_ratio)), Image.Resampling.LANCZOS)
                top = (img.height - target_height) // 2
                img = img.crop((0, top, target_width, top + target_height))
            
            image_file = tmp_path / "article_image.jpg"
            img.save(image_file, "JPEG", quality=90)
            image_paths.append(image_file)
            logging.info("Used article image as fallback")
        except Exception as exc:
            logging.debug("Failed to use article image: %s", exc)
    
    # Final fallback: solid color placeholder
    if not image_paths:
        img = Image.new("RGB", (target_width, target_height), color=(30, 30, 50))
        image_file = tmp_path / "placeholder.jpg"
        img.save(image_file, "JPEG")
        image_paths.append(image_file)
        logging.warning("Using placeholder image")
    
    return None, image_paths


def ensure_image(path: Path, article: ArticleCandidate, config: Config) -> Path:
    target_width, target_height = 1080, 1920
    
    if article.image_url:
        try:
            response = requests.get(article.image_url, timeout=10)
            response.raise_for_status()
            # Load and process image to exact 1080x1920
            img = Image.open(BytesIO(response.content))
            img = img.convert("RGB")
            
            # Resize to cover 1080x1920 while maintaining aspect ratio
            img_ratio = img.width / img.height
            target_ratio = target_width / target_height
            
            if img_ratio > target_ratio:
                # Image is wider - fit to height, crop width
                img = img.resize((int(target_height * img_ratio), target_height), Image.Resampling.LANCZOS)
                left = (img.width - target_width) // 2
                img = img.crop((left, 0, left + target_width, target_height))
            else:
                # Image is taller - fit to width, crop height
                img = img.resize((target_width, int(target_width / img_ratio)), Image.Resampling.LANCZOS)
                top = (img.height - target_height) // 2
                img = img.crop((0, top, target_width, top + target_height))
            
            img.save(path, "JPEG", quality=85)
            return path
        except Exception as exc:
            logging.warning("Failed to download/process article image: %s, trying stock media", exc)
    
    # Try stock media first (more engaging)
    keywords = extract_keywords_for_search(article)
    stock_urls = fetch_stock_media(keywords, config, media_type="photo", count=1)
    
    if stock_urls:
        try:
            response = requests.get(stock_urls[0], timeout=15)
            response.raise_for_status()
            img = Image.open(BytesIO(response.content))
            img = img.convert("RGB")
            
            img_ratio = img.width / img.height
            target_ratio = target_width / target_height
            
            if img_ratio > target_ratio:
                img = img.resize((int(target_height * img_ratio), target_height), Image.Resampling.LANCZOS)
                left = (img.width - target_width) // 2
                img = img.crop((left, 0, left + target_width, target_height))
            else:
                img = img.resize((target_width, int(target_width / img_ratio)), Image.Resampling.LANCZOS)
                top = (img.height - target_height) // 2
                img = img.crop((0, top, target_width, top + target_height))
            
            img.save(path, "JPEG", quality=90)
            logging.info("Used stock media image")
            return path
        except Exception as exc:
            logging.debug("Failed to fetch stock media: %s, trying article image", exc)

    # Final fallback: placeholder
    img = Image.new("RGB", (target_width, target_height), color=(20, 20, 40))
    img.save(path, "JPEG")
    return path


def generate_audio_with_gcloud_tts(script: str, output_path: Path, config: Config, max_retries: int = 3) -> Optional[Path]:
    """Generate audio narration using Google Cloud Text-to-Speech API with retry logic."""
    if not config.use_gcloud_tts:
        return None
    
    if texttospeech is None:
        logging.warning("google-cloud-texttospeech not available")
        return None
    
    # Handle credentials: could be a file path or JSON content (from GitHub secrets)
    credentials_path = config.gcloud_tts_credentials_path
    if not credentials_path or not credentials_path.strip():
        # No credentials provided, skip Google Cloud TTS
        logging.info("Google Cloud TTS credentials not provided, skipping")
        return None
    
    if credentials_path:
        # Check if it's JSON content (starts with {) or a file path
        if credentials_path.strip().startswith('{'):
            # It's JSON content, write to temp file
            import tempfile
            temp_creds_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
            try:
                temp_creds_file.write(credentials_path)
                temp_creds_file.close()
                credentials_path = temp_creds_file.name
                logging.debug("Wrote Google Cloud credentials to temporary file")
            except Exception as exc:
                logging.warning("Failed to write credentials to temp file: %s", exc)
                return None
        elif not Path(credentials_path).exists():
            # File path doesn't exist
            logging.warning("Google Cloud credentials file not found: %s", credentials_path)
            return None
        
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
    
    try:
        client = texttospeech.TextToSpeechClient()
    except Exception as exc:
        logging.warning("Failed to initialize Google Cloud TTS client: %s", exc)
        return None
    
    # Clean script for TTS (remove markdown, formatting, etc.)
    clean_script = clean_script_for_tts(script)
    if not clean_script:
        logging.warning("Empty script provided to TTS after cleaning")
        return None
    
    # Configure synthesis input
    synthesis_input = texttospeech.SynthesisInput(text=clean_script)
    
    # Configure voice (Google will infer gender from voice name)
    # Voice names like "en-US-Neural2-D" (D=male) or "en-US-Neural2-F" (F=female)
    voice = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name=config.gcloud_tts_voice_name,
    )
    
    # Configure audio encoding
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
        pitch=0.0,
    )
    
    for attempt in range(max_retries):
        try:
            # Perform TTS
            response = client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
            
            # Save audio file
            with open(output_path, "wb") as out:
                out.write(response.audio_content)
            
            # Log character usage for cost tracking
            char_count = len(clean_script)
            if output_path.exists() and output_path.stat().st_size > 0:
                logging.info("Generated audio with Google Cloud TTS (%d characters, %.2f KB)", 
                            char_count, output_path.stat().st_size / 1024)
                return output_path
            else:
                logging.warning("Audio file was not created or is empty")
                return None
                
        except Exception as exc:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff
                logging.warning("Google Cloud TTS attempt %d/%d failed: %s, retrying in %ds", 
                              attempt + 1, max_retries, exc, wait_time)
                time.sleep(wait_time)
            else:
                logging.warning("Google Cloud TTS failed after %d attempts: %s", max_retries, exc)
                return None
    
    return None


def generate_audio_with_edge_tts(script: str, output_path: Path, config: Config) -> Optional[Path]:
    """Generate audio narration using Edge-TTS (fallback)."""
    if edge_tts is None:
        return None
    
    try:
        import asyncio
        
        async def _generate():
            # Clean script for TTS (remove markdown, formatting, etc.)
            clean_script = clean_script_for_tts(script)
            if not clean_script:
                raise ValueError("Empty script after cleaning")
            
            communicate = edge_tts.Communicate(clean_script, config.tts_voice)
            await communicate.save(str(output_path))
        
        asyncio.run(_generate())
        
        if output_path.exists() and output_path.stat().st_size > 0:
            logging.info("Generated audio narration with Edge-TTS (%.2f KB)", output_path.stat().st_size / 1024)
            return output_path
        else:
            logging.warning("Audio file was not created or is empty")
            return None
    except Exception as exc:
        logging.warning("Edge-TTS audio generation failed: %s", exc)
        return None


def generate_audio(script: str, output_path: Path, config: Config) -> Optional[Path]:
    """Generate audio narration with fallback chain: Google Cloud TTS -> Edge-TTS -> None."""
    # Try Google Cloud TTS first
    if config.use_gcloud_tts:
        gcloud_audio = generate_audio_with_gcloud_tts(script, output_path, config)
        if gcloud_audio:
            return gcloud_audio
        logging.info("Falling back to Edge-TTS")
    
    # Fallback to Edge-TTS
    edge_audio = generate_audio_with_edge_tts(script, output_path, config)
    if edge_audio:
        return edge_audio
    
    # Final fallback: silent video with captions
    logging.warning("All audio generation methods failed, video will be silent with captions")
    return None




def create_rounded_background(width: int, height: int, corner_radius: int, color: tuple, opacity: float) -> Image.Image:
    """Create a rounded rectangle background with gradient effect."""
    # Create image with alpha channel
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Draw rounded rectangle
    draw.rounded_rectangle(
        [(0, 0), (width, height)],
        radius=corner_radius,
        fill=(*color, int(255 * opacity))
    )
    
    # Apply slight blur for softer edges
    img = img.filter(ImageFilter.GaussianBlur(radius=1))
    return img


def create_gradient_background(width: int, height: int, start_color: tuple, end_color: tuple, opacity: float) -> Image.Image:
    """Create a gradient background from start_color to end_color."""
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    for y in range(height):
        ratio = y / height if height > 0 else 0
        r = int(start_color[0] * (1 - ratio) + end_color[0] * ratio)
        g = int(start_color[1] * (1 - ratio) + end_color[1] * ratio)
        b = int(start_color[2] * (1 - ratio) + end_color[2] * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b, int(255 * opacity)))
    
    return img


def ease_out_cubic(progress: float) -> float:
    """Ease out cubic easing function for smooth animations."""
    return 1 - (1 - progress) ** 3


def ease_in_out(progress: float) -> float:
    """Ease in-out easing function for smooth animations."""
    return progress * progress * (3 - 2 * progress)


def create_captions(script: str, duration: float, video_size: tuple) -> List[TextClip]:
    """Create modern stylish captions with rounded backgrounds, gradients, and smooth animations."""
    captions = []
    video_width, video_height = video_size
    
    # Clean script and split into sentences
    clean_script = re.sub(r"\s+", " ", script).strip()
    phrases = re.split(r"[.!?]+\s+", clean_script)
    phrases = [p.strip() for p in phrases if p.strip() and len(p.strip()) > 10][:5]  # Max 5 captions for better coverage
    
    if not phrases:
        return captions
    
    # Calculate timing with slight overlap for smoother transitions
    total_phrase_time = duration * 0.95  # Use 95% of duration, leaving buffer
    time_per_phrase = total_phrase_time / len(phrases)
    overlap_time = 0.2  # 0.2s overlap between captions
    
    # Modern styling constants
    font_size = 50
    max_text_width = 900  # Max width for text (with margins)
    padding = 25  # Padding around text in background box
    corner_radius = 20  # Rounded corner radius
    subtitle_y_position = video_height - 320  # Position in lower third, not at bottom
    
    # Modern color scheme
    text_color = "#FFFFFF"  # Bright white
    start_bg_color = (30, 30, 50)  # Dark blue-purple
    end_bg_color = (0, 0, 0)  # Black
    bg_opacity = 0.85
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        for i, phrase in enumerate(phrases):
            # Smart truncation - break at word boundaries if needed
            if len(phrase) > 80:
                words = phrase.split()
                truncated = []
                char_count = 0
                for word in words:
                    if char_count + len(word) + 1 > 77:
                        break
                    truncated.append(word)
                    char_count += len(word) + 1
                phrase = " ".join(truncated) + "..."
            
            # Calculate timing with overlap
            start_time = i * time_per_phrase
            end_time = min((i + 1) * time_per_phrase + overlap_time, duration)
            phrase_duration = end_time - start_time
            
            if phrase_duration < 0.4:  # Skip very short captions
                continue
            
            try:
                # Create text clip first to determine size
                txt_clip = TextClip(
                    phrase,
                    fontsize=font_size,
                    color=text_color,
                    stroke_color="black",
                    stroke_width=2,
                    method="caption",
                    size=(max_text_width, None),
                    align="center",
                    font="Arial-Bold",  # Use bold for better readability
                )
                
                # Get text dimensions
                text_w, text_h = txt_clip.size
                
                # Create background box with padding
                bg_width = min(text_w + padding * 2, max_text_width + padding * 2)
                bg_height = text_h + padding * 2
                
                # Create gradient background
                bg_image = create_gradient_background(
                    bg_width, bg_height, start_bg_color, end_bg_color, bg_opacity
                )
                
                # Create rounded rectangle mask
                mask = Image.new('L', (bg_width, bg_height), 0)
                mask_draw = ImageDraw.Draw(mask)
                mask_draw.rounded_rectangle(
                    [(0, 0), (bg_width, bg_height)],
                    radius=corner_radius,
                    fill=255  # White for visible area
                )
                
                # Apply rounded mask to gradient background
                final_bg = Image.new('RGBA', (bg_width, bg_height), (0, 0, 0, 0))
                final_bg.paste(bg_image, (0, 0))
                final_bg.putalpha(mask)
                
                # Save background to temp file
                bg_path = tmp_path / f"subtitle_bg_{i}.png"
                final_bg.save(bg_path, "PNG")
                
                # Create background clip
                bg_clip = ImageClip(str(bg_path)).set_duration(phrase_duration).set_start(start_time)
                
                # Position background (will be animated)
                final_y = subtitle_y_position
                start_y = final_y + 30  # Start 30px below final position
                
                # Animate position with easing
                def animate_y(t):
                    if t < 0.4:  # Animation duration
                        progress = t / 0.4
                        eased = ease_out_cubic(progress)
                        return start_y + (final_y - start_y) * eased
                    return final_y
                
                # Animate scale
                def animate_scale(t):
                    if t < 0.4:  # Animation duration
                        progress = t / 0.4
                        eased = ease_out_cubic(progress)
                        return 0.9 + 0.1 * eased  # Scale from 0.9 to 1.0
                    return 1.0
                
                bg_clip = bg_clip.set_position(lambda t: ("center", animate_y(t)))
                bg_clip = bg_clip.resize(lambda t: animate_scale(t))
                
                # Position text centered on background
                # Both background and text use "center" alignment, so they align at the same y position
                txt_clip = txt_clip.set_duration(phrase_duration).set_start(start_time)
                txt_clip = txt_clip.set_position(lambda t: ("center", animate_y(t)))
                
                # Enhanced fade transitions
                fade_in_duration = 0.3
                fade_out_duration = 0.4
                
                # Apply fades
                bg_clip = bg_clip.fadein(fade_in_duration).fadeout(fade_out_duration)
                txt_clip = txt_clip.fadein(fade_in_duration).fadeout(fade_out_duration)
                
                # Add both background and text to captions
                captions.append(bg_clip)
                captions.append(txt_clip)
                
            except Exception as exc:
                logging.warning("Failed to create modern caption '%s': %s", phrase[:30], exc)
                # Fallback to simple text clip
                try:
                    simple_clip = TextClip(
                        phrase,
                        fontsize=font_size,
                        color=text_color,
                        stroke_color="black",
                        stroke_width=2,
                        method="caption",
                        size=(max_text_width, None),
                        align="center",
                    ).set_duration(phrase_duration).set_start(start_time).set_position(("center", subtitle_y_position))
                    simple_clip = simple_clip.fadein(0.3).fadeout(0.4)
                    captions.append(simple_clip)
                except:
                    logging.debug("Failed to create fallback caption")
    
    return captions


def assemble_video(article: ArticleCandidate, script: str, config: Config) -> Path:
    output_path = config.output_dir / "daily_tech_news.mp4"
    video_size = (1080, 1920)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Generate audio first to determine duration
        audio_path = tmp_path / "narration.mp3"
        audio_clip = None
        audio_duration = 40.0  # Default duration
        
        generated_audio = generate_audio(script, audio_path, config)
        if generated_audio and generated_audio.exists():
            try:
                audio_clip = AudioFileClip(str(audio_path))
                audio_duration = audio_clip.duration
                if audio_duration < 15.0:
                    logging.warning("Audio too short (%.2fs), using minimum duration", audio_duration)
                    audio_duration = 15.0
                elif audio_duration > 60.0:
                    logging.warning("Audio too long (%.2fs), will trim to 60s", audio_duration)
                    audio_duration = 60.0
                logging.info("Audio duration: %.2f seconds", audio_duration)
            except Exception as exc:
                logging.warning("Failed to load audio: %s, using default duration", exc)
                audio_clip = None
                if audio_clip:
                    audio_clip.close()
                    audio_clip = None

        duration_seconds = max(15.0, min(60.0, audio_duration))  # Clamp between 15-60 seconds

        # Prepare stock media (videos or multiple images)
        stock_video_path, stock_image_paths = prepare_stock_media(article, config, tmp_path, count=5)
        
        # Create video clips
        video_clips = []
        clips_to_close = []  # Track clips that need explicit closing
        
        if stock_video_path:
            # Use stock video
            try:
                # Verify video file exists and is not empty
                if not Path(stock_video_path).exists() or Path(stock_video_path).stat().st_size == 0:
                    raise ValueError("Stock video file is missing or empty")
                
                stock_video = VideoFileClip(stock_video_path)
                clips_to_close.append(stock_video)  # Track for cleanup
                
                # Resize video to 1080x1920
                stock_video = stock_video.resize(height=1920)
                if stock_video.w > 1080:
                    stock_video = stock_video.crop(x_center=stock_video.w/2, width=1080)
                elif stock_video.w < 1080:
                    stock_video = stock_video.resize(width=1080)
                
                # Loop or trim video to match audio duration
                if stock_video.duration < duration_seconds:
                    # Loop the video
                    loops_needed = int(duration_seconds / stock_video.duration) + 1
                    stock_video = concatenate_videoclips([stock_video] * loops_needed)
                    clips_to_close.append(stock_video)  # Track concatenated clip
                
                stock_video = stock_video.subclip(0, duration_seconds)
                video_clips.append(stock_video)
                logging.info("Using stock video for entire duration")
            except Exception as exc:
                logging.warning("Failed to use stock video: %s, falling back to images", exc)
                # Close any clips that were created before the error
                for clip in clips_to_close:
                    try:
                        clip.close()
                    except:
                        pass
                clips_to_close.clear()
                stock_video_path = None
        
        if not stock_video_path and stock_image_paths:
            # Use multiple images with transitions
            clips_per_image = max(1, len(stock_image_paths))
            duration_per_image = duration_seconds / len(stock_image_paths)
            
            for i, image_path in enumerate(stock_image_paths):
                try:
                    img_clip = ImageClip(str(image_path)).set_duration(duration_per_image)
                    
                    # Apply Ken Burns effect: zoom in/out
                    zoom_start = 1.1 if i % 2 == 0 else 1.0
                    zoom_end = 1.0 if i % 2 == 0 else 1.1
                    img_clip = img_clip.resize(
                        lambda t: zoom_start + (zoom_end - zoom_start) * (t / duration_per_image)
                    )
                    img_clip = img_clip.set_position(("center", "center"))
                    
                    # Add fade transitions
                    if i > 0:
                        img_clip = img_clip.fadein(0.5)
                    if i < len(stock_image_paths) - 1:
                        img_clip = img_clip.fadeout(0.5)
                    
                    video_clips.append(img_clip)
                except Exception as exc:
                    logging.debug("Failed to create clip from image %d: %s", i, exc)
                    continue
            
            if not video_clips:
                # Fallback to single image
                image_path = tmp_path / "frame.jpg"
                ensure_image(image_path, article, config)
                img_clip = ImageClip(str(image_path)).set_duration(duration_seconds)
                zoom_start, zoom_end = 1.2, 1.0
                img_clip = img_clip.resize(lambda t: zoom_start - (zoom_start - zoom_end) * (t / duration_seconds))
                img_clip = img_clip.set_position(("center", "center"))
                video_clips.append(img_clip)
        
        if not video_clips:
            # Final fallback: single placeholder image
            image_path = tmp_path / "frame.jpg"
            ensure_image(image_path, article, config)
            img_clip = ImageClip(str(image_path)).set_duration(duration_seconds)
            zoom_start, zoom_end = 1.2, 1.0
            img_clip = img_clip.resize(lambda t: zoom_start - (zoom_start - zoom_end) * (t / duration_seconds))
            img_clip = img_clip.set_position(("center", "center"))
            video_clips.append(img_clip)
        
        # Concatenate all video clips
        if len(video_clips) > 1:
            base_video = concatenate_videoclips(video_clips, method="compose")
            # Note: concatenate_videoclips creates a new clip, original clips still need closing
        else:
            base_video = video_clips[0]
        
        # Ensure exact duration
        if base_video.duration != duration_seconds:
            base_video = base_video.subclip(0, duration_seconds)

        # Create captions
        captions = create_captions(script, duration_seconds, video_size)
        
        # Create subtle gradient overlay at bottom (lighter since subtitles have their own backgrounds)
        # This helps with readability on bright backgrounds
        overlay = ColorClip(
            size=(video_size[0], 200), 
            color=(0, 0, 0)
        ).set_opacity(0.2).set_position(("center", video_size[1] - 200)).set_duration(duration_seconds)

        # Composite all elements
        clips = [base_video, overlay] + captions
        composite = CompositeVideoClip(clips, size=video_size)
        composite = composite.set_duration(duration_seconds)
        
        # Add audio if available
        if audio_clip:
            # Trim audio to match video duration if needed
            if audio_clip.duration > duration_seconds:
                audio_clip = audio_clip.subclip(0, duration_seconds)
            composite = composite.set_audio(audio_clip)
        
        # Add fade in/out
        composite = composite.fadein(0.5).fadeout(0.5)

        # Write video file with error handling
        try:
            composite.write_videofile(
                str(output_path),
                fps=24,
                codec="libx264",
                audio_codec="aac" if audio_clip else None,
                bitrate="5000k",
                verbose=False,
                logger=None,
                preset="medium",  # Balance between speed and file size
            )
            
            # Verify output file
            if not output_path.exists():
                raise FileNotFoundError("Video file was not created")
            
            file_size_mb = output_path.stat().st_size / (1024 * 1024)
            if file_size_mb > 50:
                logging.warning("Video file size (%.2f MB) exceeds TikTok limit (50 MB)", file_size_mb)
            else:
                logging.info("Video file size: %.2f MB", file_size_mb)
                
        except Exception as exc:
            logging.error("Failed to write video file: %s", exc)
            raise
        
        finally:
            # Cleanup: Close all clips to release file handles (critical on Windows)
            if audio_clip:
                try:
                    audio_clip.close()
                except:
                    pass
            
            if composite:
                try:
                    composite.close()
                except:
                    pass
            
            if base_video:
                try:
                    base_video.close()
                except:
                    pass
            
            # Close all tracked clips
            for clip in clips_to_close:
                try:
                    clip.close()
                except:
                    pass
            
            # Close any remaining clips in video_clips
            for clip in video_clips:
                if hasattr(clip, 'close'):
                    try:
                        clip.close()
                    except:
                        pass
            
            # Small delay to ensure file handles are released (Windows-specific)
            time.sleep(0.1)

    logging.info("Video assembled at %s", output_path)
    return output_path


def main() -> None:
    setup_logging()
    setup_nltk()  # Initialize NLTK data for newspaper3k
    config = load_config()
    
    if config.ai_only_mode:
        logging.info("Running in AI-only mode (minimum %d AI keywords required)", config.min_ai_keywords)

    story = select_top_story(DEFAULT_SOURCES, config.max_articles, config)
    if not story:
        if config.ai_only_mode:
            logging.error("Pipeline halted: no AI-related stories available")
        else:
            logging.error("Pipeline halted: no story available")
        return

    script = generate_script(story, config)
    metadata = generate_metadata(story, script)
    video_path = assemble_video(story, script, config)

    logging.info("Video generation completed successfully")
    logging.info("Video path: %s", video_path)
    logging.info("Metadata: %s", metadata)
    
    # Upload to platforms
    youtube_video_id = None
    tiktok_video_id = None
    
    # Upload to YouTube
    if config.upload_to_youtube:
        logging.info("Attempting to upload to YouTube...")
        try:
            youtube_video_id = upload_to_youtube(
                video_path,
                metadata["title"],
                metadata["description"],
                metadata["tags"],
                config,
            )
            if youtube_video_id:
                logging.info("YouTube upload successful: https://www.youtube.com/watch?v=%s", youtube_video_id)
            else:
                logging.warning("YouTube upload failed, but continuing with pipeline")
        except Exception as exc:
            logging.error("YouTube upload error: %s", exc, exc_info=True)
            logging.warning("Continuing with pipeline despite YouTube upload failure")
    else:
        logging.info("YouTube upload disabled in config")
    
    # Upload to TikTok
    if config.upload_to_tiktok:
        logging.info("Attempting to upload to TikTok...")
        try:
            tiktok_video_id = upload_to_tiktok(
                video_path,
                metadata["title"],
                config,
            )
            if tiktok_video_id:
                logging.info("TikTok upload successful: %s", tiktok_video_id)
            else:
                logging.warning("TikTok upload failed, but continuing with pipeline")
        except Exception as exc:
            logging.error("TikTok upload error: %s", exc, exc_info=True)
            logging.warning("Continuing with pipeline despite TikTok upload failure")
    else:
        logging.info("TikTok upload disabled in config")
    
    # Final summary
    logging.info("=" * 60)
    logging.info("Pipeline completed successfully")
    logging.info("=" * 60)
    logging.info("Video file: %s", video_path)
    if youtube_video_id:
        logging.info("YouTube: https://www.youtube.com/watch?v=%s", youtube_video_id)
    if tiktok_video_id:
        logging.info("TikTok: %s", tiktok_video_id)
    if not youtube_video_id and not tiktok_video_id:
        logging.warning("No videos were uploaded to any platform")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()

