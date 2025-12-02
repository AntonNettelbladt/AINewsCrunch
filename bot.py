import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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
class WordTiming:
    """Represents a word with its timing information."""
    word: str
    start_time: float
    end_time: float


@dataclass
class Phrase:
    """Represents a caption phrase with timing and words."""
    text: str
    start_time: float
    end_time: float
    words: List[WordTiming]


@dataclass
class Config:
    output_dir: Path
    max_articles: int = 10
    max_videos_per_day: int = 1  # Number of videos to create per run (workflow runs multiple times per day)
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
    gcloud_tts_voice_name: str = "Achird"  # Chirp3-HD voice
    gcloud_tts_language_code: str = "en-US"  # Language/locale code (e.g., "en-US", "en-GB", "es-ES")
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
    # Caption settings
    enable_captions: bool = True
    caption_font_size: int = 60
    caption_max_chars_per_line: int = 40
    caption_fade_duration: float = 0.3
    caption_position: str = "bottom"  # bottom/center/top


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
        name="Reddit: LocalLLaMA",
        source_type="reddit",
        weight=1.5,
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
        rss_url="https://venturebeat.com/feed/",
        source_type="rss",
        weight=1.5,
    ),
    SourceFeed(
        name="The Decoder",
        rss_url="https://the-decoder.com/feed/",
        source_type="rss",
        weight=1.4,
    ),
    SourceFeed(
        name="AI News",
        rss_url="https://www.artificialintelligence-news.com/feed/",
        source_type="rss",
        weight=1.4,
    ),
    SourceFeed(
        name="The Verge AI",
        rss_url="https://www.theverge.com/rss/index.xml",
        source_type="rss",
        weight=1.3,
    ),
    SourceFeed(
        name="ZDNet AI",
        rss_url="https://www.zdnet.com/topic/artificial-intelligence/rss.xml",
        source_type="rss",
        weight=1.3,
    ),
    SourceFeed(
        name="IEEE Spectrum AI",
        rss_url="https://spectrum.ieee.org/rss/topic/artificial-intelligence/fulltext",
        source_type="rss",
        weight=1.4,
    ),
    SourceFeed(
        name="AI Business",
        rss_url="https://aibusiness.com/feed",
        source_type="rss",
        weight=1.3,
    ),
    SourceFeed(
        name="Synced Review",
        rss_url="https://syncedreview.com/feed/",
        source_type="rss",
        weight=1.3,
    ),
    SourceFeed(
        name="Towards Data Science",
        rss_url="https://towardsdatascience.com/feed",
        source_type="rss",
        weight=1.2,
    ),
    SourceFeed(
        name="Analytics Insight",
        rss_url="https://www.analyticsinsight.net/feed/",
        source_type="rss",
        weight=1.3,
    ),
    SourceFeed(
        name="AI Trends",
        rss_url="https://www.aitrends.com/feed/",
        source_type="rss",
        weight=1.3,
    ),
    SourceFeed(
        name="KDnuggets",
        rss_url="https://www.kdnuggets.com/feed",
        source_type="rss",
        weight=1.2,
    ),
    SourceFeed(
        name="ScienceDaily AI",
        rss_url="https://www.sciencedaily.com/rss/computers_math/artificial_intelligence.xml",
        source_type="rss",
        weight=1.2,
    ),
    SourceFeed(
        name="Wired AI",
        rss_url="https://www.wired.com/feed/category/artificial-intelligence/rss",
        source_type="rss",
        weight=1.1,
    ),
    SourceFeed(
        name="NVIDIA Blog",
        rss_url="https://feeds.feedburner.com/nvidiablog",
        source_type="rss",
        weight=1.4,
    ),
    SourceFeed(
        name="NVIDIA News",
        rss_url="https://nvidianews.nvidia.com/news/feed",
        source_type="rss",
        weight=1.3,
    ),
    SourceFeed(
        name="OpenAI Blog",
        rss_url="https://openai.com/blog/rss.xml",
        source_type="rss",
        weight=1.5,
    ),
    SourceFeed(
        name="Anthropic Blog",
        rss_url="https://www.anthropic.com/index.xml",
        source_type="rss",
        weight=1.4,
    ),
    SourceFeed(
        name="Google AI Blog",
        rss_url="https://ai.googleblog.com/feeds/posts/default",
        source_type="rss",
        weight=1.4,
    ),
    SourceFeed(
        name="Microsoft AI Blog",
        rss_url="https://blogs.microsoft.com/ai/feed/",
        source_type="rss",
        weight=1.3,
    ),
    SourceFeed(
        name="Meta AI Research",
        rss_url="https://ai.meta.com/blog/feed/",
        source_type="rss",
        weight=1.3,
    ),
    
    # Tier 3: General Tech (Filtered for AI)
    SourceFeed(
        name="TechCrunch",
        rss_url="https://techcrunch.com/feed/",
        source_type="rss",
        weight=1.2,
    ),
    SourceFeed(
        name="The Information",
        rss_url="https://www.theinformation.com/feed",
        source_type="rss",
        weight=1.1,
    ),
    SourceFeed(
        name="TechRadar AI",
        rss_url="https://www.techradar.com/rss/news/artificial-intelligence",
        source_type="rss",
        weight=1.1,
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
    SourceFeed(
        name="Forbes AI",
        rss_url="https://www.forbes.com/real-time/feed2/",
        source_type="rss",
        weight=1.0,
    ),
    SourceFeed(
        name="Reuters Technology",
        rss_url="https://www.reutersagency.com/feed/?best-topics=tech&post_type=best",
        source_type="rss",
        weight=0.9,
    ),
    SourceFeed(
        name="Bloomberg Technology",
        rss_url="https://www.bloomberg.com/feeds/sites/2/technology.rss",
        source_type="rss",
        weight=0.9,
    ),
    SourceFeed(
        name="Engadget",
        rss_url="https://www.engadget.com/rss.xml",
        source_type="rss",
        weight=0.9,
    ),
    SourceFeed(
        name="Gizmodo",
        rss_url="https://gizmodo.com/rss",
        source_type="rss",
        weight=0.9,
    ),
    SourceFeed(
        name="Fast Company",
        rss_url="https://www.fastcompany.com/feed",
        source_type="rss",
        weight=0.8,
    ),
    SourceFeed(
        name="Quartz",
        rss_url="https://qz.com/feed/",
        source_type="rss",
        weight=0.8,
    ),
    SourceFeed(
        name="The Next Web",
        rss_url="https://thenextweb.com/feed",
        source_type="rss",
        weight=0.8,
    ),
    SourceFeed(
        name="CNET Technology",
        rss_url="https://www.cnet.com/rss/news/",
        source_type="rss",
        weight=0.8,
    ),
    SourceFeed(
        name="Digital Trends",
        rss_url="https://www.digitaltrends.com/feed/",
        source_type="rss",
        weight=0.7,
    ),
]

EXCLUSION_KEYWORD_WEIGHTS = {
    # High severity (3-4 points) - Strong indicators of low-value content
    "black friday": 4, "cyber monday": 4, "flash sale": 4, "clearance": 3,
    "buy now": 3, "add to cart": 3, "get it now": 3, "order now": 3, "where to buy": 3,
    
    # Medium-high severity (2-3 points)
    "sale": 2, "deal": 2, "discount": 2, "coupon": 2, "promo": 2,
    "cheap": 2, "bargain": 2, "on sale": 2, "sponsored": 3, "advertisement": 3,
    "ad": 2, "sponsor": 2, "promoted": 2, "promotion": 2,
    
    # Medium severity (1-2 points) - Can appear in legitimate content
    "review": 1, "hands-on": 1, "unboxing": 2, "first look": 1, "buyer's guide": 2,
    "best ": 1, "top ": 1, "ranking": 1, "comparison": 1, "vs ": 1, "versus": 1,
    "price": 1, "cost": 1, "affordable": 1, "budget": 1, "pricing": 1,
    "shop": 2, "shopping": 2, "purchase": 1, "order": 1, "save": 1,
    "limited time": 2, "special offer": 2, "buy it": 2,
}

PRACTICAL_AI_USE_CASES = {
    # Programming/AI coding (high boost)
    "ai coding": 3, "code generation": 3, "programming assistant": 3, "coding assistant": 3,
    "ai developer": 2, "copilot": 3, "ai tool": 2, "ai plugin": 2, "ai extension": 2,
    
    # AI art/image generation (high boost)
    "ai art": 3, "image generation": 3, "ai image": 3, "ai drawing": 2, "ai design": 2,
    "ai graphics": 2, "text to image": 3, "image to image": 2,
    
    # AI chat/conversation (high boost)
    "ai chat": 3, "chatbot": 3, "ai conversation": 2, "ai assistant": 2, "ai companion": 2,
    
    # AI video generation (high boost)
    "ai video": 3, "video generation": 3, "text to video": 3, "ai animation": 2, "video ai": 2,
    
    # AI music/audio (high boost)
    "ai music": 3, "ai audio": 2, "music generation": 3, "ai voice": 2, "voice generation": 2,
    "text to speech": 2, "ai voiceover": 3, "voice cloning": 2,
    
    # AI productivity (medium boost)
    "ai workflow": 2, "ai automation": 2, "ai productivity": 2, "ai app": 2, "ai software": 2,
}

ACADEMIC_RESEARCH_INDICATORS = {
    # High severity - likely academic paper
    "arxiv": 4, "preprint": 3, "peer reviewed": 3, "academic paper": 4, "research paper": 3,
    "scientific paper": 3, "journal": 3, "conference paper": 3, "doi:": 3,
    "citation": 2, "references": 2,
    
    # Medium severity - research-focused
    "methodology": 2, "hypothesis": 2, "experiment": 2, "dataset": 2,
    "theoretical": 2, "framework": 1,  # framework can be practical too
}

LEGITIMATE_PATTERNS = {
    "deal": [
        r'\bdeal\s+(with|between|announced|signed|reached|struck|closed|finalized)',
        r'\b(partnership|merger|acquisition|business|investment|funding)\s+deal',
        r'\bdeal\s+(worth|valued|valued at|amounting to)',
        r'\b(multi.?million|billion)\s+deal',
    ],
    "review": [
        r'\b(review|reviewed|reviewing)\s+(of|the|findings|research|study|paper|data|literature)',
        r'\b(peer|scientific|academic|research|systematic|meta)\s+review',
        r'\b(review|reviewed)\s+(by|from|according|published)',
        r'\breview\s+(process|board|committee)',
    ],
    "best": [
        r'\bbest\s+(practices|methods|approaches|ways|strategies|solutions|techniques|tools)',
        r'\bbest\s+(for|in|at|to|way|approach)\s+',
        r'\b(best|top)\s+(ai|tech|technology|companies|models|frameworks|libraries)',
        r'\b(best|top)\s+\d+\s+(ai|tech|tools|frameworks)',
    ],
    "price": [
        r'\bprice\s+(of|for|per|tag|point|target|range|action|stability)',
        r'\b(pricing|cost)\s+(strategy|model|analysis|structure|tier|plan)',
        r'\b(cost|price)\s+(to|of|for)\s+(train|develop|build|create|run|operate)',
        r'\b(cost|price)\s+(efficiency|effective|reduction|optimization)',
        r'\bmarket\s+price',
    ],
    "promotion": [
        r'\bpromotion\s+(to|of|within|at|from)',
        r'\b(promoted|promoting)\s+(to|as|within|from)',
        r'\b(job|career|executive|employee|staff)\s+promotion',
        r'\bpromotion\s+(campaign|strategy|effort)',
    ],
    "sale": [
        r'\b(sales|selling)\s+(team|force|department|process|strategy|growth)',
        r'\b(sales|selling)\s+(of|for|to)\s+',
        r'\bannual\s+sales',
        r'\bsales\s+(figures|data|numbers|report|target)',
    ],
    "discount": [
        r'\b(discount|discounting)\s+(rate|factor|model|method)',
        r'\b(discount|discounting)\s+(for|on|applied)',
    ],
    "sponsored": [
        r'\bsponsored\s+(by|content|post|article)',
        r'\b(sponsor|sponsoring)\s+(organization|company|institution)',
    ],
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
    
    # Log credential status (consolidated)
    creds_status = []
    if yt_client_id and yt_client_secret and yt_refresh_token:
        creds_status.append("YouTube")
    if tiktok_client_key and tiktok_client_secret and tiktok_access_token:
        creds_status.append("TikTok")
    if gcloud_creds:
        creds_status.append("Google Cloud TTS")
    if creds_status:
        logging.info("Credentials configured: %s", ", ".join(creds_status))
    else:
        logging.warning("No credentials configured")

    config = Config(
        output_dir=output_dir,
        max_videos_per_day=int(os.getenv("MAX_VIDEOS_PER_DAY", "1")),  # Default to 1 video per run
        max_script_points=int(os.getenv("MAX_SCRIPT_POINTS", "3")),
        max_script_words=int(os.getenv("MAX_SCRIPT_WORDS", "150")),
        tts_voice=os.getenv("TTS_VOICE") or "en-US-AriaNeural",  # Default Edge-TTS voice (handles empty strings)
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
        gcloud_tts_voice_name=os.getenv("GCLOUD_TTS_VOICE") or "Achird",  # Default to Chirp3-HD Achird voice (handles empty strings)
        gcloud_tts_language_code=os.getenv("GCLOUD_TTS_LANGUAGE") or "en-US",  # Language/locale code (handles empty strings)
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
        # Caption settings
        enable_captions=os.getenv("ENABLE_CAPTIONS", "true").lower() == "true",
        caption_font_size=int(os.getenv("CAPTION_FONT_SIZE", "60")),
        caption_max_chars_per_line=int(os.getenv("CAPTION_MAX_CHARS_PER_LINE", "40")),
        caption_fade_duration=float(os.getenv("CAPTION_FADE_DURATION", "0.3")),
        caption_position=os.getenv("CAPTION_POSITION", "bottom"),
    )
    logging.debug("Loaded config: %s", config)
    return config


# User agents for better scraping
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]


def get_headers(custom_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Get default headers with random user agent, optionally merged with custom headers."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8,application/rss+xml,application/atom+xml",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.com/",  # Make requests look like they came from Google
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
    # Source fetching log removed for cleaner output - only log failures
    
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
        # Standard RSS feed (handles RSS 2.0, Atom, and other formats)
        headers = get_headers(source.headers) if source.headers else get_headers()
        response = fetch_with_retry(source.rss_url, headers=headers)
        if not response:
            return []

        links: List[str] = []
        try:
            # Try to parse as XML
            root = ET.fromstring(response.content)
            
            # Handle RSS 2.0 format (items in <item> tags)
            for item in root.findall(".//item")[:max_entries]:
                link_element = item.find("link")
                if link_element is not None and link_element.text:
                    links.append(link_element.text.strip())
                else:
                    # Some RSS feeds use <guid> as link
                    guid_element = item.find("guid")
                    if guid_element is not None and guid_element.text:
                        links.append(guid_element.text.strip())
            
            # Handle Atom format (entries in <entry> tags)
            if not links:
                for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry")[:max_entries]:
                    link_element = entry.find("{http://www.w3.org/2005/Atom}link")
                    if link_element is not None:
                        # Atom links can be in href attribute
                        link_url = link_element.get("href") or link_element.text
                        if link_url:
                            links.append(link_url.strip())
                    else:
                        # Try id element in Atom
                        id_element = entry.find("{http://www.w3.org/2005/Atom}id")
                        if id_element is not None and id_element.text:
                            links.append(id_element.text.strip())
            
            # Handle RSS 1.0 format (items in <item> tags with different namespace)
            if not links:
                for item in root.findall(".//{http://purl.org/rss/1.0/}item")[:max_entries]:
                    link_element = item.find("{http://purl.org/rss/1.0/}link")
                    if link_element is not None and link_element.text:
                        links.append(link_element.text.strip())
            
            if links:
                logging.debug("Found %d links for %s", len(links), source.name)
            else:
                logging.warning("No links found in RSS feed for %s (may be empty or different format)", source.name)
            
        except ET.ParseError as exc:
            # Try to handle HTML responses that might be returned instead of RSS
            content_str = response.content.decode('utf-8', errors='ignore')[:500]
            if '<html' in content_str.lower() or '<!doctype' in content_str.lower():
                logging.warning("RSS feed for %s returned HTML instead of XML (may be blocked or URL incorrect)", source.name)
            else:
                logging.warning("Malformed RSS for %s: %s (content preview: %s)", source.name, exc, content_str[:200])
            return []
        except Exception as exc:
            logging.warning("Error parsing RSS for %s: %s", source.name, exc)
            return []
        
        return links
    
    return []


def is_shopping_context(text: str, keyword: str, position: int) -> bool:
    """Check if keyword appears in shopping/sales context."""
    shopping_indicators = ["buy", "shop", "cart", "checkout", "purchase", "order", "sale", "discount"]
    context_window = text[max(0, position-50):min(len(text), position+50)]
    return any(indicator in context_window for indicator in shopping_indicators)


def is_news_context(text: str, keyword: str, position: int) -> bool:
    """Check if keyword appears in legitimate news context."""
    news_indicators = ["announces", "reports", "reveals", "according", "study", "research", 
                       "findings", "analysis", "data", "company", "partnership", "acquisition"]
    context_window = text[max(0, position-50):min(len(text), position+50)]
    return any(indicator in context_window for indicator in news_indicators)


def has_ai_tech_context(text: str, keyword: str, position: int) -> bool:
    """Check if keyword appears near AI/tech terms (whitelist exception)."""
    ai_tech_terms = ["ai", "artificial intelligence", "machine learning", "neural", "model", 
                     "algorithm", "tech", "technology", "software", "platform", "system"]
    context_window = text[max(0, position-100):min(len(text), position+100)]
    return any(term in context_window for term in ai_tech_terms)


def has_practical_ai_focus(candidate: ArticleCandidate) -> bool:
    """Check if article focuses on practical AI applications."""
    text = f"{candidate.title} {candidate.summary}".lower()
    practical_score = 0
    for term, weight in PRACTICAL_AI_USE_CASES.items():
        if term in text:
            practical_score += weight
    return practical_score >= 3  # At least one high-weight term or multiple medium terms


def is_overly_academic(candidate: ArticleCandidate) -> bool:
    """Check if article is overly academic/research-focused."""
    text = f"{candidate.title} {candidate.summary}".lower()
    academic_score = 0
    for term, weight in ACADEMIC_RESEARCH_INDICATORS.items():
        if term in text:
            academic_score += weight
    # Exclude if high academic score AND no practical AI focus
    if academic_score >= 4 and not has_practical_ai_focus(candidate):
        return True
    return False


def get_domain_from_url(url: str) -> str:
    """Extract domain from URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        domain = parsed.netloc.lower()
        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except:
        return ""


def is_shopping_domain(url: str) -> bool:
    """Check if URL is from a known shopping/retail domain."""
    domain = get_domain_from_url(url)
    shopping_domains = [
        "amazon.com", "ebay.com", "etsy.com", "shopify.com", "alibaba.com",
        "walmart.com", "target.com", "bestbuy.com", "newegg.com", "overstock.com",
        "zappos.com", "wayfair.com", "homedepot.com", "lowes.com", "costco.com",
        "aliexpress.com", "wish.com", "groupon.com", "livingsocial.com",
        "dealnews.com", "slickdeals.net", "retailmenot.com", "honey.com",
    ]
    return any(shop_domain in domain for shop_domain in shopping_domains)


def has_negation_nearby(text: str, position: int, window: int = 30) -> bool:
    """Check if there's a negation word near the keyword position."""
    negation_words = ["not", "no", "never", "none", "neither", "without", "lack", "free from"]
    context_window = text[max(0, position-window):min(len(text), position+window)]
    # Check for negation patterns
    negation_patterns = [
        r'\b(not|no|never)\s+\w+\s+(sale|deal|discount|promotion|ad|sponsored)',
        r'\b(without|free from|lack of)\s+(ads?|sponsors?|promotions?)',
    ]
    for pattern in negation_patterns:
        if re.search(pattern, context_window, re.IGNORECASE):
            return True
    # Check for negation words in context window
    return any(neg_word in context_window for neg_word in negation_words)


def get_sentence_boundaries(text: str) -> List[Tuple[int, int]]:
    """Get sentence boundaries (start, end positions) in text."""
    # Simple sentence boundary detection using punctuation
    sentences = []
    start = 0
    for match in re.finditer(r'[.!?]+\s+', text):
        end = match.end()
        sentences.append((start, end))
        start = end
    if start < len(text):
        sentences.append((start, len(text)))
    return sentences


def is_in_same_sentence(text: str, pos1: int, pos2: int) -> bool:
    """Check if two positions are in the same sentence."""
    sentences = get_sentence_boundaries(text)
    for start, end in sentences:
        if start <= pos1 < end and start <= pos2 < end:
            return True
    return False


def count_keyword_clusters(text: str, keywords: List[str], max_distance: int = 100) -> int:
    """Count clusters of exclusion keywords appearing close together."""
    keyword_positions = []
    for keyword in keywords:
        keyword_lower = keyword.lower()
        if " " not in keyword:
            pattern = r'\b' + re.escape(keyword_lower) + r'\b'
        else:
            pattern = re.escape(keyword_lower)
        for match in re.finditer(pattern, text):
            keyword_positions.append((match.start(), keyword))
    
    if len(keyword_positions) < 2:
        return 0
    
    # Sort by position
    keyword_positions.sort()
    
    clusters = 0
    i = 0
    while i < len(keyword_positions) - 1:
        cluster_size = 1
        j = i + 1
        while j < len(keyword_positions):
            if keyword_positions[j][0] - keyword_positions[i][0] <= max_distance:
                cluster_size += 1
                j += 1
            else:
                break
        if cluster_size >= 2:
            clusters += 1
        i = j if cluster_size > 1 else i + 1
    
    return clusters


def calculate_promotional_density(text: str) -> float:
    """Calculate density of promotional language in text."""
    promotional_phrases = [
        "limited time", "act now", "don't miss", "exclusive", "special offer",
        "one-time", "today only", "while supplies last", "hurry", "urgent",
        "last chance", "expires soon", "order now", "buy now", "click here",
        "sign up now", "free trial", "no credit card", "money back guarantee",
    ]
    words = text.split()
    if not words:
        return 0.0
    
    promotional_count = 0
    text_lower = text.lower()
    for phrase in promotional_phrases:
        promotional_count += text_lower.count(phrase.lower())
    
    return (promotional_count / len(words)) * 100 if words else 0.0


def is_article_too_short(candidate: ArticleCandidate) -> bool:
    """Check if article is suspiciously short (likely an ad or low-value content)."""
    word_count = len(candidate.text.split()) if candidate.text else 0
    # Very short articles (< 150 words) with exclusion keywords are suspicious
    return word_count < 150


def analyze_title_vs_body(candidate: ArticleCandidate, keyword: str) -> int:
    """Analyze if keyword appears in title (more significant) vs body."""
    title_lower = candidate.title.lower() if candidate.title else ""
    summary_lower = candidate.summary.lower() if candidate.summary else ""
    text_lower = candidate.text.lower() if candidate.text else ""
    
    keyword_lower = keyword.lower()
    if " " not in keyword:
        pattern = r'\b' + re.escape(keyword_lower) + r'\b'
    else:
        pattern = re.escape(keyword_lower)
    
    in_title = bool(re.search(pattern, title_lower))
    in_summary = bool(re.search(pattern, summary_lower))
    in_body = bool(re.search(pattern, text_lower))
    
    # Title matches are most significant (weight x2)
    # Summary matches are significant (weight x1.5)
    # Body matches are normal (weight x1)
    if in_title:
        return 2
    elif in_summary:
        return 1
    elif in_body:
        return 0
    return 0


def should_exclude_article(candidate: ArticleCandidate) -> Optional[str]:
    """Check if article should be excluded using weighted scoring system.
    Returns exclusion reason if article should be excluded, None otherwise."""
    full_text = f"{candidate.title} {candidate.summary} {candidate.text}".lower()
    text = f"{candidate.title} {candidate.summary}".lower()
    
    # First check: Exclude overly academic papers without practical focus
    if is_overly_academic(candidate):
        return "overly academic/research paper without practical AI focus"
    
    # Check if URL is from shopping domain (strong indicator)
    if is_shopping_domain(candidate.url):
        return "shopping/retail domain"
    
    # Check if article is suspiciously short with exclusion keywords
    if is_article_too_short(candidate):
        # Only exclude if it also has exclusion keywords
        has_exclusion_keywords = any(
            kw.lower() in text for kw in ["sale", "deal", "discount", "buy now", "order now", "sponsored", "ad"]
        )
        if has_exclusion_keywords:
            return "suspiciously short article with exclusion keywords"
    
    exclusion_score = 0
    exclusion_reasons = []
    exclusion_threshold = 5  # Only exclude if score >= threshold
    
    # Check if article has practical AI focus (reduces exclusion score)
    has_practical_focus = has_practical_ai_focus(candidate)
    practical_boost = -3 if has_practical_focus else 0  # Increased boost for practical articles
    
    # Calculate promotional language density
    promotional_density = calculate_promotional_density(full_text)
    if promotional_density > 2.0:  # More than 2% promotional phrases
        exclusion_score += int(promotional_density)
        exclusion_reasons.append("high promotional density")
    
    # Track keyword positions for clustering analysis
    keyword_matches = []
    
    # Check each keyword with context awareness
    for keyword, base_weight in EXCLUSION_KEYWORD_WEIGHTS.items():
        keyword_lower = keyword.lower()
        
        # Use word boundaries for single words
        if " " not in keyword:
            pattern = r'\b' + re.escape(keyword_lower) + r'\b'
            matches = list(re.finditer(pattern, full_text))
        else:
            matches = list(re.finditer(re.escape(keyword_lower), full_text))
        
        if not matches:
            continue
        
        # Check each match for context
        for match in matches:
            position = match.start()
            weight = base_weight
            
            # Check for negation nearby (reduces weight significantly)
            if has_negation_nearby(full_text, position):
                weight = 0  # Don't count negated keywords
                continue
            
            # Apply title/body weighting (keywords in title are more significant)
            title_weight_boost = analyze_title_vs_body(candidate, keyword)
            if title_weight_boost > 0:
                weight = int(weight * (1 + title_weight_boost * 0.5))  # 50% boost for title, 25% for summary
            
            # Apply context-based adjustments
            if keyword in LEGITIMATE_PATTERNS:
                for pattern in LEGITIMATE_PATTERNS[keyword]:
                    if re.search(pattern, full_text[max(0, position-30):min(len(full_text), position+30)]):
                        weight = 0  # Don't count this match
                        break
            
            # Whitelist: Reduce weight if in AI/tech context
            if has_ai_tech_context(full_text, keyword, position):
                weight = max(0, weight - 1)
            
            # Reduce weight if in news context (not shopping)
            if is_news_context(full_text, keyword, position) and not is_shopping_context(full_text, keyword, position):
                weight = max(0, weight - 1)
            
            # Increase weight if in shopping context
            if is_shopping_context(full_text, keyword, position):
                weight = min(weight + 1, 4)
            
            if weight > 0:
                exclusion_score += weight
                keyword_matches.append((position, keyword, weight))
                if keyword not in exclusion_reasons:
                    exclusion_reasons.append(keyword)
    
    # Check for keyword clustering (multiple exclusion keywords close together)
    if len(keyword_matches) >= 2:
        keywords_found = [kw for _, kw, _ in keyword_matches]
        clusters = count_keyword_clusters(full_text, keywords_found, max_distance=150)
        if clusters > 0:
            exclusion_score += clusters * 2  # Add 2 points per cluster
            exclusion_reasons.append(f"{clusters} keyword cluster(s)")
    
    # Apply practical AI boost (reduces exclusion score)
    exclusion_score = max(0, exclusion_score + practical_boost)
    
    # Dynamic threshold adjustment based on article quality
    # Longer, well-written articles get benefit of the doubt
    word_count = len(candidate.text.split()) if candidate.text else 0
    if word_count > 500 and has_practical_focus:
        exclusion_threshold += 2  # Raise threshold for longer practical articles
    
    # Only exclude if score exceeds threshold
    if exclusion_score >= exclusion_threshold:
        reason_parts = [f"exclusion score {exclusion_score}"]
        if exclusion_reasons:
            reason_parts.append(f"keywords: {', '.join(exclusion_reasons[:3])}")
        return " | ".join(reason_parts)
    
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
            # Add small random delay between source requests to avoid rate limiting
            if sources_succeeded > 0 or sources_failed > 0:  # Don't delay the first source
                time.sleep(random.uniform(0.5, 2.0))
            
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
    
    # Consolidated collection stats
    if config.ai_only_mode:
        logging.info("Collected %d AI articles from %d sources (%d excluded, %d major news)", 
                    ai_articles_found, sources_succeeded, excluded_count, major_news_count)
    else:
        logging.info("Collected %d articles from %d sources (%d excluded)", 
                    len(candidates), sources_succeeded, excluded_count)
    
    # Ensure we have at least some articles before proceeding
    if len(candidates) == 0 and sources_succeeded == 0:
        logging.error("All sources failed! Check your internet connection and source availability.")
    
    return candidates


def load_covered_stories(config: Config) -> Set[str]:
    """Load set of already covered story URLs from JSON file.
    
    Returns:
        Set of story URLs that have already been covered
    """
    covered_file = config.output_dir / "covered_stories.json"
    
    if not covered_file.exists():
        logging.debug("No covered stories file found, starting fresh")
        return set()
    
    try:
        with open(covered_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Extract URLs from the data structure
        # Data format: {url: {title, date_covered, ...}}
        covered_urls = set(data.keys())
        
        # Clean up old entries (older than 30 days)
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)
        cleaned_data = {}
        for url, info in data.items():
            try:
                date_covered = datetime.fromisoformat(info.get('date_covered', ''))
                if date_covered >= cutoff_date:
                    cleaned_data[url] = info
            except (ValueError, TypeError):
                # Keep entries with invalid dates (better safe than sorry)
                cleaned_data[url] = info
        
        # Save cleaned data if we removed entries
        if len(cleaned_data) < len(data):
            with open(covered_file, 'w', encoding='utf-8') as f:
                json.dump(cleaned_data, f, indent=2, ensure_ascii=False)
            logging.debug("Cleaned up %d old covered stories", len(data) - len(cleaned_data))
        
        logging.debug("Loaded %d covered stories from history", len(cleaned_data))
        return set(cleaned_data.keys())
        
    except (json.JSONDecodeError, IOError, Exception) as exc:
        logging.warning("Failed to load covered stories file: %s", exc)
        return set()


def load_used_media_ids(config: Config) -> Set[str]:
    """Load set of already used stock media IDs to avoid reuse.
    
    Returns:
        Set of media IDs (from Pexels, Pixabay, Unsplash) that have been used
    """
    used_media_file = config.output_dir / "used_media_ids.json"
    
    if not used_media_file.exists():
        logging.debug("No used media file found, starting fresh")
        return set()
    
    try:
        with open(used_media_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Data format: {media_id: date_used}
        used_ids = set(data.keys())
        
        # Clean up old entries (older than 3 days to allow some reuse after a while)
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=3)
        cleaned_data = {}
        for media_id, date_str in data.items():
            try:
                date_used = datetime.fromisoformat(date_str)
                if date_used >= cutoff_date:
                    cleaned_data[media_id] = date_str
            except (ValueError, TypeError):
                # Keep entries with invalid dates
                cleaned_data[media_id] = date_str
        
        # Save cleaned data if we removed entries
        if len(cleaned_data) < len(data):
            with open(used_media_file, 'w', encoding='utf-8') as f:
                json.dump(cleaned_data, f, indent=2, ensure_ascii=False)
            logging.debug("Cleaned up %d old used media IDs", len(data) - len(cleaned_data))
        
        logging.debug("Loaded %d used media IDs from history", len(cleaned_data))
        return set(cleaned_data.keys())
        
    except (json.JSONDecodeError, IOError, Exception) as exc:
        logging.warning("Failed to load used media file: %s", exc)
        return set()


def save_used_media_ids(media_ids: List[str], config: Config) -> None:
    """Save media IDs that were used to avoid reuse.
    
    Args:
        media_ids: List of media IDs to mark as used
        config: Config object with output directory
    """
    if not media_ids:
        return
    
    used_media_file = config.output_dir / "used_media_ids.json"
    
    # Load existing data
    data = {}
    if used_media_file.exists():
        try:
            with open(used_media_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as exc:
            logging.warning("Failed to load used media for update: %s", exc)
            data = {}
    
    # Add new entries
    current_date = datetime.now(timezone.utc).isoformat()
    for media_id in media_ids:
        if media_id:  # Only save non-empty IDs
            data[media_id] = current_date
    
    # Save updated data
    try:
        config.output_dir.mkdir(parents=True, exist_ok=True)
        with open(used_media_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        # Media IDs saved silently (debug only if needed)
    except (IOError, Exception) as exc:
        logging.warning("Failed to save used media IDs: %s", exc)


def save_covered_story(story: ArticleCandidate, config: Config, youtube_id: Optional[str] = None, tiktok_id: Optional[str] = None) -> None:
    """Save a story as covered in the JSON file.
    
    Args:
        story: The article candidate that was covered
        config: Config object with output directory
        youtube_id: Optional YouTube video ID if uploaded
        tiktok_id: Optional TikTok video ID if uploaded
    """
    covered_file = config.output_dir / "covered_stories.json"
    
    # Load existing data
    data = {}
    if covered_file.exists():
        try:
            with open(covered_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as exc:
            logging.warning("Failed to load covered stories for update: %s", exc)
            data = {}
    
    # Add or update entry
    data[story.url] = {
        'title': story.title,
        'date_covered': datetime.now(timezone.utc).isoformat(),
        'source': story.source,
        'youtube_id': youtube_id,
        'tiktok_id': tiktok_id,
    }
    
    # Save updated data
    try:
        # Ensure output directory exists
        config.output_dir.mkdir(parents=True, exist_ok=True)
        
        with open(covered_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        logging.debug("Saved story as covered: %s", story.url[:60])
    except (IOError, Exception) as exc:
        logging.warning("Failed to save covered story: %s", exc)


def select_top_story(sources: List[SourceFeed], max_articles: int, config: Config) -> Optional[ArticleCandidate]:
    """Select the top story (backward compatibility)."""
    stories = select_top_stories(sources, max_articles, 1, config)
    return stories[0] if stories else None


def select_top_stories(sources: List[SourceFeed], max_articles: int, max_stories: int, config: Config) -> List[ArticleCandidate]:
    """Select top N unique stories for video generation, excluding already covered stories."""
    # Load already covered stories
    covered_urls = load_covered_stories(config)
    
    candidates = collect_candidates(sources, max_articles, config)
    if not candidates:
        if config.ai_only_mode:
            logging.error("No AI-related articles available for selection")
        else:
            logging.error("No articles available for selection")
        return []
    
    # Filter out already covered stories
    if covered_urls:
        original_count = len(candidates)
        candidates = [c for c in candidates if c.url not in covered_urls]
        filtered_count = original_count - len(candidates)
        if filtered_count > 0:
            logging.debug("Filtered out %d already covered stories", filtered_count)
    
    if not candidates:
        logging.warning("All candidates were already covered. No new stories available.")
        return []
    
    ranked = rank_articles(candidates, sources, config)
    if not ranked:
        if config.ai_only_mode:
            logging.error("No AI-related articles available for selection")
        else:
            logging.error("No articles available for selection")
        return []
    
    # Select top N unique stories (avoid duplicates by URL)
    selected_stories = []
    seen_urls = set()
    
    for story in ranked:
        if len(selected_stories) >= max_stories:
            break
        # Skip if we've already used this URL in this run, or if it's already covered
        if story.url not in seen_urls and story.url not in covered_urls:
            selected_stories.append(story)
            seen_urls.add(story.url)
            density = calculate_ai_density(story) if config.ai_only_mode else 0.0
            ai_keywords_found = [k for k in AI_KEYWORDS.keys() if k.lower() in story.title.lower() or k.lower() in story.summary.lower()][:3] if config.ai_only_mode else []
            logging.info("Selected: '%s' (score: %.2f)", story.title[:60], story.score)
    
    return selected_stories


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
    
    # Replace "AI" with "Artificial Intelligence" for better TTS pronunciation
    # Use word boundaries to match "AI" as a standalone word, not part of other words
    # Handle possessive case first: "AI's" -> "Artificial Intelligence's"
    script = re.sub(r'\bAI\'s\b', "Artificial Intelligence's", script, flags=re.IGNORECASE)
    # Then handle standalone "AI" -> "Artificial Intelligence"
    script = re.sub(r'\bAI\b', 'Artificial Intelligence', script, flags=re.IGNORECASE)
    
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
                
                # Log token usage if available (debug only)
                if hasattr(response, 'usage_metadata'):
                    usage = response.usage_metadata
                    logging.debug("Gemini API usage: %d prompt + %d completion tokens", 
                               usage.prompt_token_count if hasattr(usage, 'prompt_token_count') else 0,
                               usage.candidates_token_count if hasattr(usage, 'candidates_token_count') else 0)
                
                logging.info("Generated script using Gemini API (%d words)", word_count)
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
        logging.debug("Falling back to template-based script generation")
    
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
    
    logging.info("Generated script using template (%d words)", word_count)
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


def create_thumbnail(article: ArticleCandidate, title: str, output_path: Path, config: Config) -> Optional[Path]:
    """Create a captivating thumbnail with bold text and cool gradient for YouTube Shorts.
    
    Args:
        article: Article candidate with image URL
        title: Video title (will be used as thumbnail text)
        output_path: Path where thumbnail should be saved
        config: Config object
        
    Returns:
        Path to thumbnail image if successful, None otherwise
    """
    try:
        # YouTube Shorts thumbnail size: 1080x1920 (9:16 vertical format)
        thumbnail_width = 1080
        thumbnail_height = 1920
        
        # Create base image
        img = Image.new('RGB', (thumbnail_width, thumbnail_height), color='#1a1a2e')
        draw = ImageDraw.Draw(img)
        
        # Cool gradient color combinations
        gradients = [
            # Purple to Blue
            ((138, 43, 226), (30, 144, 255)),
            # Deep Blue to Cyan
            ((25, 25, 112), (0, 191, 255)),
            # Dark Purple to Pink
            ((75, 0, 130), (255, 20, 147)),
            # Navy to Teal
            ((0, 0, 128), (0, 128, 128)),
            # Indigo to Purple
            ((75, 0, 130), (138, 43, 226)),
            # Dark Blue to Light Blue
            ((0, 0, 139), (135, 206, 250)),
            # Magenta to Purple
            ((255, 0, 255), (128, 0, 128)),
            # Orange to Red
            ((255, 140, 0), (220, 20, 60)),
            # Teal to Green
            ((0, 128, 128), (0, 255, 127)),
            # Dark Red to Orange
            ((139, 0, 0), (255, 165, 0)),
        ]
        
        # Randomly select a gradient
        start_color, end_color = random.choice(gradients)
        
        # Create vertical gradient background
        for y in range(thumbnail_height):
            ratio = y / thumbnail_height
            r = int(start_color[0] + (end_color[0] - start_color[0]) * ratio)
            g = int(start_color[1] + (end_color[1] - start_color[1]) * ratio)
            b = int(start_color[2] + (end_color[2] - start_color[2]) * ratio)
            draw.line([(0, y), (thumbnail_width, y)], fill=(r, g, b))
        
        # Get Coiny font for bold text
        font_path = get_coiny_font_path(config)
        
        # Prepare text - use a shorter, punchier version of the title
        # Limit to ~60 characters for vertical format
        thumbnail_text = title[:60]
        if len(title) > 60:
            thumbnail_text = title[:57] + "..."
        
        # Split text into lines if needed (max 3 lines for vertical format)
        words = thumbnail_text.split()
        lines = []
        current_line = ""
        for word in words:
            test_line = current_line + " " + word if current_line else word
            if len(test_line) <= 35:  # ~35 chars per line for vertical format
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)
        lines = lines[:3]  # Max 3 lines for vertical format
        
        # Draw text with bold styling
        try:
            if font_path:
                # Use Coiny font - larger size for vertical format
                font_size = 120 if len(lines) == 1 else (100 if len(lines) == 2 else 85)
                try:
                    font = ImageFont.truetype(font_path, font_size)
                except:
                    font = ImageFont.load_default()
            else:
                # Fallback to default bold font
                try:
                    font = ImageFont.truetype("arial.ttf", 120)
                except:
                    font = ImageFont.load_default()
        except:
            font = ImageFont.load_default()
        
        # Calculate text position (centered vertically)
        total_text_height = len(lines) * 140  # Approximate line height
        text_y_start = thumbnail_height // 2 - total_text_height // 2
        
        # Draw text with outline (shadow effect for readability)
        for line_idx, line in enumerate(lines):
            # Get text dimensions
            bbox = draw.textbbox((0, 0), line, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            
            # Center horizontally
            text_x = (thumbnail_width - text_width) // 2
            text_y = text_y_start + (line_idx * 140)
            
            # Draw black outline (shadow) - draw multiple times for thicker outline
            outline_color = (0, 0, 0)
            outline_thickness = 4
            for adj in [(-outline_thickness, -outline_thickness), (-outline_thickness, outline_thickness), 
                       (outline_thickness, -outline_thickness), (outline_thickness, outline_thickness),
                       (-outline_thickness, 0), (outline_thickness, 0), (0, -outline_thickness), (0, outline_thickness),
                       (-2, -2), (-2, 2), (2, -2), (2, 2)]:
                draw.text((text_x + adj[0], text_y + adj[1]), line, font=font, fill=outline_color)
            
            # Draw white text on top
            draw.text((text_x, text_y), line, font=font, fill='#FFFFFF')
        
        # Save thumbnail
        img.save(str(output_path), 'PNG', quality=95)
        logging.debug("Created thumbnail: %s", output_path.name)
        return output_path
        
    except Exception as exc:
        logging.error("Failed to create thumbnail: %s", exc, exc_info=True)
        return None


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


def upload_to_youtube(video_path: Path, title: str, description: str, tags: str, config: Config, thumbnail_path: Optional[Path] = None, max_retries: int = 3) -> Optional[str]:
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
            if attempt == 0:
                logging.info("Uploading video to YouTube...")
            else:
                logging.info("Retrying YouTube upload (attempt %d/%d)...", attempt + 1, max_retries)
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
                                    if progress % 25 == 0 or progress == 100:  # Only log at 25%, 50%, 75%, 100%
                                        logging.info("Upload progress: %d%%", progress)
                    except Exception:
                        # Ignore errors when trying to get progress info
                        pass
            
            if "id" in response:
                video_id = response["id"]
                logging.info("Video uploaded to YouTube: %s", video_id)
                
                # Upload thumbnail if provided
                if thumbnail_path and thumbnail_path.exists():
                    # YouTube requires a short delay after video upload before thumbnail can be set
                    # Wait a few seconds to ensure video is processed
                    time.sleep(5)  # YouTube processing delay
                    
                    # Retry thumbnail upload up to 3 times
                    thumbnail_uploaded = False
                    for thumb_attempt in range(3):
                        try:
                            if thumb_attempt > 0:
                                logging.info("Retrying thumbnail upload (attempt %d/3)...", thumb_attempt + 1)
                            
                            # Verify thumbnail file is valid and is PNG format
                            try:
                                img = Image.open(thumbnail_path)
                                if img.format != 'PNG':
                                    logging.warning("Thumbnail is not PNG format (%s), converting to PNG...", img.format)
                                    # Convert to PNG if needed
                                    png_path = thumbnail_path.with_suffix('.png')
                                    img.save(png_path, 'PNG')
                                    thumbnail_path = png_path
                                    logging.debug("Converted thumbnail to PNG")
                                img.close()  # Close the image file
                            except Exception as img_exc:
                                logging.debug("Thumbnail validation warning: %s", img_exc)
                                # Continue anyway - YouTube API will reject if invalid
                            
                            # Upload thumbnail
                            youtube.thumbnails().set(
                                videoId=video_id,
                                media_body=MediaFileUpload(str(thumbnail_path), mimetype='image/png', resumable=False)
                            ).execute()
                            
                            logging.debug("Thumbnail uploaded successfully")
                            thumbnail_uploaded = True
                            break
                            
                        except HttpError as exc:
                            error_details = exc.error_details if hasattr(exc, 'error_details') else str(exc)
                            if exc.resp.status == 404:
                                # Video might not be ready yet, wait longer and retry
                                if thumb_attempt < 2:
                                    wait_time = 5 * (thumb_attempt + 1)  # 5, 10, 15 seconds
                                    logging.warning("Video not ready for thumbnail upload (404), waiting %d seconds before retry...", wait_time)
                                    time.sleep(wait_time)
                                else:
                                    logging.error("Failed to upload thumbnail after retries: Video not found (404)")
                            elif exc.resp.status == 403:
                                logging.error("Permission denied for thumbnail upload (403): %s", error_details)
                                break  # Don't retry on permission errors
                            else:
                                logging.warning("Thumbnail upload failed (attempt %d/3): %s", thumb_attempt + 1, error_details)
                                if thumb_attempt < 2:
                                    time.sleep(3)
                        except Exception as exc:
                            logging.warning("Thumbnail upload error (attempt %d/3): %s", thumb_attempt + 1, exc)
                            if thumb_attempt < 2:
                                time.sleep(3)
                    
                    if not thumbnail_uploaded:
                        logging.warning("Failed to upload thumbnail after all retries, but video upload was successful")
                elif thumbnail_path:
                    logging.warning("Thumbnail path provided but file does not exist: %s", thumbnail_path)
                
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
                logging.debug("Uploading video in chunks (%.2f MB)...", file_size_mb)
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
                logging.debug("Uploading video file (%.2f MB)...", file_size_mb)
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
            
            logging.debug("Video file uploaded successfully")
            
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
                    logging.info("Video published to TikTok: %s", video_id)
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
    """Extract specific, article-focused searchable keywords from article for stock media search.
    Returns more specific keywords based on article content to get better matching stock media."""
    # Combine title, summary, and first paragraph for better context
    text = f"{article.title} {article.summary} {article.text[:500]}".lower()
    keywords = []
    
    # Extract specific technology/product names (more specific than generic AI terms)
    specific_terms = []
    
    # Look for specific AI models, products, or technologies mentioned
    specific_patterns = [
        r'\b(gpt-\d+|claude|gemini|llama|mistral|palm|dall-e|midjourney|stable diffusion|flux)\b',
        r'\b(transformer|diffusion|gan|rnn|cnn|bert|t5)\b',
        r'\b(chatbot|assistant|agent|autonomous|robotics|computer vision|nlp)\b',
        r'\b(gpu|cpu|tensor|neural|algorithm|model|training|inference)\b',
    ]
    
    for pattern in specific_patterns:
        matches = re.findall(pattern, text)
        specific_terms.extend([m for m in matches if m not in specific_terms])
        if len(specific_terms) >= 3:
            break
    
    # Extract company/product names
    company_patterns = [
        r'\b(openai|anthropic|google|microsoft|meta|facebook|nvidia|amd|intel|apple|amazon|tesla)\b',
        r'\b(deepmind|stability ai|midjourney|cohere|mistral ai|hugging face)\b',
    ]
    
    for pattern in company_patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            if match not in specific_terms:
                specific_terms.append(match)
                if len(specific_terms) >= 4:
                    break
        if len(specific_terms) >= 4:
            break
    
    # Extract action/application keywords from title (what is the AI doing?)
    action_keywords = []
    action_patterns = [
        r'\b(generate|create|detect|analyze|predict|recognize|translate|summarize|optimize|automate)\b',
        r'\b(breakthrough|launch|release|announce|develop|train|deploy|integrate)\b',
    ]
    
    for pattern in action_patterns:
        matches = re.findall(pattern, text)
        action_keywords.extend([m for m in matches if m not in action_keywords])
        if len(action_keywords) >= 2:
            break
    
    # Build keyword list: specific terms + action + AI context
    if specific_terms:
        keywords.extend(specific_terms[:2])  # Use 2 most specific terms
    
    if action_keywords:
        keywords.append(action_keywords[0])  # Add one action keyword
    
    # Add AI context if not already present
    has_ai_context = any(kw in text for kw in ["ai", "artificial intelligence", "machine learning"])
    if has_ai_context and not any("ai" in kw or "intelligence" in kw or "learning" in kw for kw in keywords):
        keywords.append("ai")
    
    # Fallback to more specific AI terms if no keywords found
    if not keywords:
        # Try to extract from title words
        title_words = [w for w in article.title.lower().split() if len(w) > 4]
        if title_words:
            keywords = title_words[:2] + ["ai"]
        else:
            keywords = ["artificial intelligence", "technology"]
    
    # Remove duplicates while preserving order
    seen = set()
    unique_keywords = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique_keywords.append(kw)
    
    # Return 2-3 most relevant keywords for better search specificity
    return unique_keywords[:3]


# Keywords that should exclude a video from being selected (contextually irrelevant)
VIDEO_EXCLUSION_KEYWORDS = {
    "fitness", "gym", "workout", "exercise", "training", "sport", "athlete", "muscle", 
    "yoga", "running", "cycling", "swimming", "basketball", "football", "soccer",
    "cooking", "recipe", "food", "restaurant", "chef", "kitchen",
    "fashion", "makeup", "beauty", "cosmetics", "model",
    "travel", "vacation", "beach", "tourism", "hotel",
    "pets", "dog", "cat", "animal", "wildlife",
    "nature", "landscape", "mountains", "ocean", "forest",
    "medical", "hospital", "surgery", "doctor", "patient",
    "education", "classroom", "student", "school", "teacher",
    "entertainment", "movie", "music", "concert", "party",
}


def calculate_video_relevance_score(video: dict, article_keywords: List[str], article_text: str) -> float:
    """Calculate relevance score for a video based on its metadata and article keywords.
    
    Args:
        video: Video metadata dict from Pexels API
        article_keywords: List of keywords extracted from article
        article_text: Article title + summary for context matching
        
    Returns:
        Relevance score (0.0 to 1.0), higher is better. Negative score means reject.
    """
    score = 0.0
    
    # Extract all available metadata from video object
    video_metadata_parts = []
    
    # Video URL often contains descriptive paths
    if "url" in video:
        video_metadata_parts.append(str(video.get("url", "")))
    
    # User information
    user = video.get("user", {})
    if isinstance(user, dict):
        if "name" in user:
            video_metadata_parts.append(str(user.get("name", "")))
    
    # Video ID might be in URL format
    if "id" in video:
        video_metadata_parts.append(str(video.get("id", "")))
    
    # Check video_files for any metadata
    video_files = video.get("video_files", [])
    for vf in video_files:
        if isinstance(vf, dict) and "link" in vf:
            video_metadata_parts.append(str(vf.get("link", "")))
    
    # Video tags if available
    if "tags" in video:
        tags = video.get("tags", [])
        if isinstance(tags, list):
            video_metadata_parts.extend([str(tag).lower() for tag in tags if tag])
    
    # Combine all metadata into searchable string
    video_metadata = " ".join(video_metadata_parts).lower()
    
    # Extract words from URL paths (Pexels URLs often have descriptive paths)
    # e.g., /videos/technology-computer-screen-12345/
    url_paths = [part for part in video_metadata.split("/") if len(part) > 2]
    video_metadata += " " + " ".join(url_paths)
    
    # Check for exclusion keywords - if found, reject immediately
    article_text_lower = article_text.lower()
    for exclusion_kw in VIDEO_EXCLUSION_KEYWORDS:
        exclusion_lower = exclusion_kw.lower()
        if exclusion_lower in video_metadata:
            # Check if article is actually about this topic (e.g., "AI fitness app")
            # Only exclude if the article is NOT about this topic
            if exclusion_lower not in article_text_lower:
                logging.debug("Rejecting video: contains exclusion keyword '%s'", exclusion_kw)
                return -1.0  # Reject immediately
    
    # Score based on keyword matches in video metadata
    for keyword in article_keywords:
        if not keyword:
            continue
            
        keyword_lower = keyword.lower()
        
        # Exact keyword match in metadata (high score)
        if keyword_lower in video_metadata:
            score += 0.4
        
        # Partial keyword match (for compound terms like "machine learning")
        keyword_parts = keyword_lower.split()
        if len(keyword_parts) > 1:
            matching_parts = sum(1 for part in keyword_parts if len(part) > 3 and part in video_metadata)
            if matching_parts > 0:
                score += 0.2 * (matching_parts / len(keyword_parts))
        
        # Check for keyword stems/partials
        if len(keyword_lower) > 4:
            # Check if significant parts of keyword appear
            if keyword_lower[:4] in video_metadata or keyword_lower[-4:] in video_metadata:
                score += 0.1
    
    # Boost score for technology-related terms in metadata
    tech_terms = ["technology", "tech", "computer", "digital", "software", "ai", "artificial", "intelligence",
                  "machine", "learning", "innovation", "data", "code", "programming", "robot", "neural",
                  "algorithm", "chip", "processor", "screen", "device", "laptop", "smartphone", "code",
                  "network", "internet", "cyber", "electronic", "circuit", "binary", "server"]
    
    tech_matches = sum(1 for term in tech_terms if term in video_metadata)
    if tech_matches > 0:
        # Only boost if article is actually tech-related
        if any(tech in article_text_lower for tech in ["ai", "technology", "tech", "computer", "software", "digital", "artificial intelligence"]):
            score += min(0.3, tech_matches * 0.1)  # Cap boost at 0.3
    
    # Penalize generic/abstract terms (unless we have no better options)
    generic_terms = ["abstract", "pattern", "texture", "background", "gradient", "color", "art", "design"]
    generic_matches = sum(1 for term in generic_terms if term in video_metadata)
    if generic_matches > 0 and score < 0.3:
        score -= 0.15 * generic_matches  # Only penalize if score is already low
    
    # Normalize score to 0.0-1.0 range
    final_score = min(1.0, max(0.0, score))
    
    # Log low-scoring videos for debugging
    if final_score < 0.3:
        logging.debug("Low relevance video (score: %.2f): %s", final_score, video_metadata[:100])
    
    return final_score


def generate_search_queries(article: ArticleCandidate, keywords: List[str]) -> List[str]:
    """Generate multiple search query variations for better stock media matching.
    
    Args:
        article: Article candidate
        keywords: Extracted keywords
        
    Returns:
        List of search queries ordered by specificity (most specific first)
    """
    queries = []
    text_lower = f"{article.title} {article.summary}".lower()
    
    # Query 1: Most specific - combine keywords with tech context
    if keywords:
        specific_query = " ".join(keywords[:2])
        if any(kw in text_lower for kw in ["ai", "artificial intelligence", "machine learning"]):
            specific_query += " technology"
        queries.append(specific_query)
    
    # Query 2: Tech-focused if AI-related
    if any(kw in text_lower for kw in ["ai", "artificial intelligence", "machine learning", "gpt", "claude"]):
        if keywords:
            tech_query = f"{keywords[0]} artificial intelligence" if keywords else "artificial intelligence technology"
            queries.append(tech_query)
        else:
            queries.append("artificial intelligence technology")
    
    # Query 3: Generic tech fallback
    if keywords:
        queries.append(keywords[0] if keywords else "technology")
    else:
        queries.append("technology computer")
    
    # Query 4: Ultra-generic fallback
    queries.append("technology")
    
    # Remove duplicates while preserving order
    seen = set()
    unique_queries = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique_queries.append(q)
    
    return unique_queries[:4]  # Return up to 4 query variations


def fetch_stock_video(keywords: List[str], config: Config, count: int = 3, used_media_ids: Optional[Set[str]] = None, article: Optional[ArticleCandidate] = None) -> List[Tuple[str, str]]:
    """Fetch multiple stock videos from Pexels API with relevance filtering and reuse prevention.
    
    Args:
        keywords: Search keywords
        config: Config object with API keys
        count: Number of videos to fetch (default: 3)
        used_media_ids: Set of already-used media IDs to exclude
        article: Optional article for relevance scoring
    
    Returns:
        List of tuples (video_url, media_id) for tracking
    """
    if used_media_ids is None:
        used_media_ids = set()
    
    video_results = []
    article_text = ""
    if article:
        article_text = f"{article.title} {article.summary}".lower()
    
    # Generate multiple search queries for better matching
    search_queries = [ " ".join(keywords) if keywords else "technology" ]
    if article:
        search_queries = generate_search_queries(article, keywords)
    
    # Try Pexels videos with relevance filtering
    if config.pexels_api_key:
        all_candidate_videos = []
        
        # Try multiple search queries
        for search_query in search_queries:
            try:
                url = "https://api.pexels.com/videos/search"
                headers = {"Authorization": config.pexels_api_key}
                
                # Request more results to have options after filtering
                per_page = min(count * 10, 80)  # Request more for better filtering
                
                # Start from page 1 for most relevant results (only randomize if needed)
                params = {
                    "query": search_query,
                    "per_page": per_page,
                    "orientation": "portrait",
                    "page": 1  # Start with most relevant results
                }
                
                response = requests.get(url, headers=headers, params=params, timeout=10)
                response.raise_for_status()
                if response.status_code == 200:
                    data = response.json()
                    videos = data.get("videos", [])
                    
                    # Score and filter videos by relevance
                    scored_videos = []
                    for video in videos:
                        video_id = f"pexels_{video.get('id', '')}"
                        
                        # Skip if already used
                        if video_id in used_media_ids:
                            continue
                        
                        # Calculate relevance score
                        relevance_score = 0.5  # Default neutral score
                        if article:
                            relevance_score = calculate_video_relevance_score(video, keywords, article_text)
                            # Reject videos with negative scores (excluded keywords)
                            if relevance_score < 0:
                                continue
                        
                        video_files = video.get("video_files", [])
                        if video_files:
                            # Prefer HD quality, fallback to any available
                            hd_video = next((vf for vf in video_files if vf.get("quality") == "hd"), None)
                            video_url = (hd_video or video_files[0]).get("link")
                            if video_url:
                                scored_videos.append((video, video_url, video_id, relevance_score))
                    
                    # Sort by relevance score (highest first)
                    scored_videos.sort(key=lambda x: x[3], reverse=True)
                    all_candidate_videos.extend(scored_videos)
                    
                    # If we found enough highly relevant videos, stop searching
                    high_relevance = [v for v in scored_videos if v[3] >= 0.5]
                    if len(high_relevance) >= count:
                        break
                        
            except requests.RequestException as exc:
                logging.debug("Pexels video API request failed for query '%s': %s", search_query, exc)
                continue
            except Exception as exc:
                logging.debug("Pexels video API error for query '%s': %s", search_query, exc)
                continue
        
        # Remove duplicates by video_id while preserving order
        seen_ids = set()
        unique_videos = []
        for video, video_url, video_id, score in all_candidate_videos:
            if video_id not in seen_ids:
                seen_ids.add(video_id)
                unique_videos.append((video, video_url, video_id, score))
        
        # Sort by relevance and take top results
        unique_videos.sort(key=lambda x: x[3], reverse=True)
        
        # Filter out low-relevance videos (score < 0.2) unless we don't have enough
        filtered_videos = [v for v in unique_videos if v[3] >= 0.2]
        if len(filtered_videos) < count:
            # If we don't have enough, include lower scoring ones
            filtered_videos = unique_videos[:count * 2]
        
        # Take top N videos
        for video, video_url, video_id, score in filtered_videos[:count]:
            video_results.append((video_url, video_id))
            if len(video_results) >= count:
                break
        
        if video_results:
            logging.info("Fetched %d relevant stock video(s) from Pexels (searched %d queries)", len(video_results), len(search_queries))
            return video_results
    
    return video_results


def fetch_stock_media(keywords: List[str], config: Config, media_type: str = "photo", count: int = 1, used_media_ids: Optional[Set[str]] = None) -> List[Tuple[str, str]]:
    """Fetch stock media (images or videos) from Pexels, Pixabay, or Unsplash APIs with randomization and reuse prevention.
    Returns a list of tuples (url, media_id) for tracking."""
    if used_media_ids is None:
        used_media_ids = set()
    
    search_query = " ".join(keywords) if keywords else "technology"
    results = []
    
    if media_type == "video":
        # Try to fetch videos (article parameter not available here, will use basic filtering)
        video_results = fetch_stock_video(keywords, config, count=count, used_media_ids=used_media_ids, article=None)
        return video_results
    
    # Fetch images
    # Try Pexels first
    if config.pexels_api_key:
        try:
            url = "https://api.pexels.com/v1/search"
            headers = {"Authorization": config.pexels_api_key}
            
            # Request more results to have options after filtering used ones
            per_page = min(count * 5, 80)
            # Add randomization: use random page (1-10)
            random_page = random.randint(1, min(10, max(1, per_page // 15)))
            
            params = {
                "query": search_query,
                "per_page": per_page,
                "orientation": "portrait",
                "page": random_page
            }
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                photos = data.get("photos", [])
                
                # Shuffle photos to add more randomness
                random.shuffle(photos)
                
                for photo in photos:
                    if len(results) >= count:
                        break
                    
                    photo_id = f"pexels_{photo.get('id', '')}"
                    # Skip if already used
                    if photo_id in used_media_ids:
                        continue
                    
                    image_url = photo.get("src", {}).get("large") or photo.get("src", {}).get("original")
                    if image_url:
                        results.append((image_url, photo_id))
                
                if results:
                    logging.debug("Fetched %d stock image(s) from Pexels", len(results))
                    return results[:count]
        except requests.RequestException as exc:
            logging.debug("Pexels API request failed: %s", exc)
        except Exception as exc:
            logging.debug("Pexels API error: %s", exc)
    
    # Try Pixabay
    if config.pixabay_api_key and len(results) < count:
        try:
            url = "https://pixabay.com/api/"
            
            # Request more results and add randomization
            per_page = min(count * 5, 200)
            random_page = random.randint(1, min(10, max(1, per_page // 20)))
            
            params = {
                "key": config.pixabay_api_key,
                "q": search_query,
                "image_type": "photo",
                "orientation": "vertical",
                "per_page": per_page,
                "page": random_page,
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                hits = data.get("hits", [])
                
                # Shuffle hits to add more randomness
                random.shuffle(hits)
                
                for hit in hits:
                    if len(results) >= count:
                        break
                    
                    hit_id = f"pixabay_{hit.get('id', '')}"
                    # Skip if already used
                    if hit_id in used_media_ids:
                        continue
                    
                    image_url = hit.get("largeImageURL") or hit.get("webformatURL")
                    if image_url:
                        results.append((image_url, hit_id))
                
                if results:
                    logging.debug("Fetched %d stock image(s) from Pixabay", len(results))
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
            
            # Request more results and add randomization
            per_page = min(count * 5, 30)
            random_page = random.randint(1, min(10, max(1, per_page // 10)))
            
            params = {
                "query": search_query,
                "per_page": per_page,
                "orientation": "portrait",
                "page": random_page
            }
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            if response.status_code == 200:
                data = response.json()
                photo_results = data.get("results", [])
                
                # Shuffle results to add more randomness
                random.shuffle(photo_results)
                
                for result in photo_results:
                    if len(results) >= count:
                        break
                    
                    result_id = f"unsplash_{result.get('id', '')}"
                    # Skip if already used
                    if result_id in used_media_ids:
                        continue
                    
                    image_url = result.get("urls", {}).get("regular") or result.get("urls", {}).get("full")
                    if image_url:
                        results.append((image_url, result_id))
                
                if results:
                    logging.debug("Fetched %d stock image(s) from Unsplash", len(results))
                    return results[:count]
        except requests.RequestException as exc:
            logging.debug("Unsplash API request failed: %s", exc)
        except Exception as exc:
            logging.debug("Unsplash API error: %s", exc)
    
    if not results:
        logging.debug("No stock media available from any API")
    return results[:count]


def prepare_stock_media(article: ArticleCandidate, config: Config, tmp_path: Path, count: int = 5) -> Tuple[List[str], List[Path]]:
    """Prepare stock media (videos and images) for video assembly with reuse prevention.
    Returns: (list_of_video_paths, list_of_image_paths)"""
    keywords = extract_keywords_for_search(article)
    video_paths = []
    image_paths = []
    used_media_ids_to_save = []  # Track IDs to save after successful download
    
    # Load previously used media IDs to avoid reuse
    used_media_ids = load_used_media_ids(config)
    
    # Try to fetch multiple stock videos first (most engaging)
    if config.pexels_api_key:
        video_results = fetch_stock_video(keywords, config, count=count, used_media_ids=used_media_ids, article=article)  # Returns (url, media_id) tuples
        
        # Download videos in parallel for faster processing
        def download_video(video_data):
            index, video_url, media_id = video_data
            try:
                video_file = tmp_path / f"stock_video_{index}.mp4"
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
                    return None, None
                
                # Verify file is not empty and has reasonable size
                if video_file.stat().st_size < 1000:  # Less than 1KB is suspicious
                    logging.warning("Downloaded video file is too small, likely corrupted")
                    return None, None
                
                return str(video_file), media_id
            except Exception as exc:
                logging.warning("Failed to download stock video %d: %s", index+1, exc)
                return None, None
        
        # Download videos in parallel
        video_data_list = [(i, url, mid) for i, (url, mid) in enumerate(video_results)]
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(download_video, data): data for data in video_data_list}
            for future in as_completed(futures):
                video_path, media_id = future.result()
                if video_path and media_id:
                    video_paths.append(video_path)
                    used_media_ids_to_save.append(media_id)
        
        if video_paths:
            # Save used media IDs
            save_used_media_ids(used_media_ids_to_save, config)
            logging.info("Prepared %d stock video(s)", len(video_paths))
            return video_paths, image_paths
    
    # Fetch multiple stock images
    stock_image_results = fetch_stock_media(keywords, config, media_type="photo", count=count, used_media_ids=used_media_ids)  # Returns (url, media_id) tuples
    target_width, target_height = 1080, 1920
    
    # Download and process images in parallel for faster processing
    def download_and_process_image(image_data):
        index, image_url, media_id = image_data
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
            
            image_file = tmp_path / f"stock_image_{index}.jpg"
            img.save(image_file, "JPEG", quality=90)
            return image_file, media_id
        except Exception as exc:
            logging.debug("Failed to download/process stock image %d: %s", index, exc)
            return None, None
    
    # Process images in parallel
    image_data_list = [(i, url, mid) for i, (url, mid) in enumerate(stock_image_results)]
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(download_and_process_image, data): data for data in image_data_list}
        for future in as_completed(futures):
            image_file, media_id = future.result()
            if image_file and media_id:
                image_paths.append(image_file)
                used_media_ids_to_save.append(media_id)
    
    if image_paths:
        # Save used media IDs
        save_used_media_ids(used_media_ids_to_save, config)
        logging.info("Prepared %d stock image(s)", len(image_paths))
        return [], image_paths
    
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
    used_media_ids = load_used_media_ids(config)
    stock_results = fetch_stock_media(keywords, config, media_type="photo", count=1, used_media_ids=used_media_ids)
    
    if stock_results:
        try:
            image_url, media_id = stock_results[0]  # Unpack tuple (url, media_id)
            response = requests.get(image_url, timeout=15)
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
            # Save the used media ID
            save_used_media_ids([media_id], config)
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
    
    # Configure voice
    # Chirp3-HD voices can be specified in two ways:
    # 1. Full format: "en-US-Chirp3-HD-Achird" (model not needed)
    # 2. Short format: "Achird" (requires model="chirp-3-hd")
    # Neural2 voices use format: "en-US-Neural2-D" or "en-US-Neural2-F"
    
    # Determine if this is a Chirp3-HD voice and construct proper voice name
    is_chirp3_hd = False
    voice_name = config.gcloud_tts_voice_name
    voice_params = {
        "language_code": config.gcloud_tts_language_code,
    }
    
    # Check if voice name already contains "Chirp3-HD" in the name (full format)
    if "Chirp3-HD" in voice_name or "chirp3-hd" in voice_name.lower():
        is_chirp3_hd = True
        voice_params["name"] = voice_name
        # Full format already includes model in the name, no separate model parameter needed
    else:
        # Check if it's a known Chirp3-HD short name
        chirp3_hd_voices = ["Achird", "Aurora", "Charon", "Fenrir", "Kore", "Puck", "Rhea", "Triton"]
        if voice_name in chirp3_hd_voices:
            is_chirp3_hd = True
            # Construct full voice name: "en-US-Chirp3-HD-Achird"
            # The model is included in the voice name format, no separate model parameter needed
            voice_params["name"] = f"{config.gcloud_tts_language_code}-Chirp3-HD-{voice_name}"
        elif voice_name and not voice_name.startswith(config.gcloud_tts_language_code + "-Neural"):
            # Short name that might be Chirp3-HD, construct full name
            voice_params["name"] = f"{config.gcloud_tts_language_code}-Chirp3-HD-{voice_name}"
            is_chirp3_hd = True
        else:
            # Neural2 or other standard voices
            voice_params["name"] = voice_name
    
    # Determine TTS model for logging
    if is_chirp3_hd:
        tts_model = "Chirp3-HD"
    else:
        tts_model = "Neural2 (default)"
    
    # Log detailed TTS configuration
    logging.info("=" * 60)
    logging.info("Google Cloud TTS Configuration:")
    logging.info("  Model: %s", tts_model)
    logging.info("  Voice Name: %s", voice_params["name"])
    logging.info("  Language/Locale: %s", config.gcloud_tts_language_code)
    logging.info("  Audio Encoding: LINEAR16")
    logging.info("  Sample Rate: 44100 Hz")
    logging.info("  Speaking Rate: 1.0")
    logging.info("  Pitch: 0.0")
    logging.info("=" * 60)
    
    voice = texttospeech.VoiceSelectionParams(**voice_params)
    
    # Configure audio encoding with LINEAR16 and 44100Hz sample rate
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.LINEAR16,
        sample_rate_hertz=44100,
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
                logging.info("Successfully generated audio with Google Cloud TTS")
                logging.info("  Model: %s | Voice: %s | Language: %s", 
                            tts_model, config.gcloud_tts_voice_name, config.gcloud_tts_language_code)
                logging.info("  Characters: %d | File size: %.2f KB", 
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
        
        # Log Edge-TTS configuration
        logging.info("=" * 60)
        logging.info("Edge-TTS Configuration (Fallback):")
        logging.info("  Provider: Microsoft Edge TTS")
        logging.info("  Voice: %s", config.tts_voice)
        logging.info("  Note: Output will be converted to LINEAR16 WAV (44100 Hz) during enhancement")
        logging.info("=" * 60)
        
        async def _generate():
            # Clean script for TTS (remove markdown, formatting, etc.)
            clean_script = clean_script_for_tts(script)
            if not clean_script:
                raise ValueError("Empty script after cleaning")
            
            communicate = edge_tts.Communicate(clean_script, config.tts_voice)
            await communicate.save(str(output_path))
        
        asyncio.run(_generate())
        
        if output_path.exists() and output_path.stat().st_size > 0:
            logging.info("Successfully generated audio with Edge-TTS")
            logging.info("  Voice: %s | File size: %.2f KB", 
                        config.tts_voice, output_path.stat().st_size / 1024)
            return output_path
        else:
            logging.warning("Audio file was not created or is empty")
            return None
    except Exception as exc:
        logging.warning("Edge-TTS audio generation failed: %s", exc)
        return None


def enhance_audio_professional(raw_audio_path: Path, output_path: Path) -> Optional[Path]:
    """Apply refined professional audio processing for TTS voiceover.
    
    Enhanced processing chain for natural, clear voiceover:
    - Multi-band filtering to remove artifacts while preserving clarity
    - Sophisticated EQ to reduce harshness and enhance intelligibility
    - Advanced de-essing to tame sibilance
    - Smooth compression for consistent levels
    - Subtle saturation for warmth
    - Professional limiting for broadcast-ready levels
    
    Args:
        raw_audio_path: Path to the raw TTS-generated audio file
        output_path: Path where the enhanced audio will be saved
        
    Returns:
        Path to enhanced audio file if successful, None otherwise
    """
    if not raw_audio_path.exists() or raw_audio_path.stat().st_size == 0:
        logging.warning("Raw audio file does not exist or is empty: %s", raw_audio_path)
        return None
    
    try:
        logging.info("Applying refined audio processing for TTS...")
        
        # Refined filter chain for professional TTS voiceover
        # Each stage addresses specific TTS audio characteristics
        
        filter_chain = (
            # Stage 1: High-pass filter at 70Hz (removes low-frequency rumble and plosives)
            "highpass=f=70,"
            # Stage 2: Low-pass filter at 14kHz (smooths digital artifacts, preserves clarity)
            "lowpass=f=14000,"
            # Stage 3: Multi-band EQ - targeted frequency adjustments for TTS
            # Reduce mud in low-mids (200-400Hz range) - common TTS issue
            "equalizer=f=300:width_type=h:width=400:g=-2.0,"
            # Slight boost in vocal presence range (2-3kHz) for clarity and intelligibility
            "equalizer=f=2500:width_type=h:width=1000:g=1.2,"
            # Reduce harshness in upper mids (4-6kHz) where sibilance and digital artifacts live
            "equalizer=f=5500:width_type=h:width=2000:g=-3.0,"
            # Gentle reduction in very high frequencies (8-10kHz) to reduce digital harshness
            "equalizer=f=9000:width_type=h:width=2000:g=-1.5,"
            # Stage 4: Smooth compression for consistent levels (gentle, natural-sounding)
            "acompressor=threshold=-20dB:ratio=3.5:attack=20:release=200:makeup=1.0,"
            # Stage 5: Additional targeted compression on sibilance range (4-7kHz) for de-essing
            "asplit[main][sibilance],"
            "[sibilance]highpass=f=4000,lowpass=f=7000,acompressor=threshold=-18dB:ratio=6:attack=2:release=30[sibilance_processed],"
            "[main][sibilance_processed]amix=inputs=2:weights=0.94 0.06:duration=first,"
            # Stage 6: Professional peak limiter with lookahead (prevents clipping, maintains dynamics)
            "alimiter=level_in=1:level_out=0.95:limit=0.95:attack=7:release=50"
        )
        
        # Join filter chain into single string
        filter_chain = "".join(filter_chain)
        
        # Build FFmpeg command with LINEAR16 (PCM WAV) encoding
        cmd = [
            "ffmpeg",
            "-i", str(raw_audio_path),
            "-af", filter_chain,
            "-ar", "44100",  # 44100 Hz sample rate
            "-ac", "1",  # Mono (voiceover doesn't need stereo)
            "-f", "wav",  # WAV format (LINEAR16 PCM encoding)
            "-acodec", "pcm_s16le",  # LINEAR16 PCM encoding (16-bit little-endian)
            "-y",  # Overwrite output file
            str(output_path)
        ]
        
        # Execute FFmpeg with error handling
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False
        )
        
        if result.returncode != 0:
            logging.warning("FFmpeg audio enhancement failed: %s", result.stderr)
            # Fallback: try simpler enhancement chain
            logging.info("Attempting simplified audio enhancement as fallback...")
            return enhance_audio_simple(raw_audio_path, output_path)
        
        # Verify output file was created and has content
        if output_path.exists() and output_path.stat().st_size > 0:
            original_size = raw_audio_path.stat().st_size
            enhanced_size = output_path.stat().st_size
            logging.info("Audio enhancement completed successfully")
            logging.info("  Original size: %.2f KB | Enhanced size: %.2f KB", 
                        original_size / 1024, enhanced_size / 1024)
            return output_path
        else:
            logging.warning("Enhanced audio file was not created or is empty")
            return enhance_audio_simple(raw_audio_path, output_path)
            
    except FileNotFoundError:
        logging.warning("FFmpeg not found - audio enhancement skipped")
        # Copy raw audio to output if FFmpeg is unavailable
        try:
            shutil.copy2(raw_audio_path, output_path)
            return output_path
        except Exception as exc:
            logging.warning("Failed to copy raw audio: %s", exc)
            return None
    except Exception as exc:
        logging.warning("Audio enhancement failed: %s", exc)
        # Fallback to simple enhancement
        return enhance_audio_simple(raw_audio_path, output_path)


def enhance_audio_simple(raw_audio_path: Path, output_path: Path) -> Optional[Path]:
    """Simplified audio processing fallback using basic FFmpeg filters.
    
    Used when the main enhancement chain fails. Still provides good cleanup.
    
    Args:
        raw_audio_path: Path to the raw audio file
        output_path: Path where the enhanced audio will be saved
        
    Returns:
        Path to enhanced audio file if successful, None otherwise
    """
    try:
        # Simplified but effective filter chain
        simple_filter_parts = (
            "highpass=f=70,",
            "lowpass=f=14000,",
            "equalizer=f=5500:width_type=h:width=2000:g=-2.0,",
            "acompressor=threshold=-20dB:ratio=3:attack=15:release=150,",
            "alimiter=level_in=1:level_out=0.95:limit=0.95"
        )
        simple_filter = "".join(simple_filter_parts)
        
        cmd = [
            "ffmpeg",
            "-i", str(raw_audio_path),
            "-af", simple_filter,
            "-ar", "44100",  # 44100 Hz sample rate
            "-ac", "1",  # Mono
            "-f", "wav",  # WAV format (LINEAR16 PCM encoding)
            "-acodec", "pcm_s16le",  # LINEAR16 PCM encoding (16-bit little-endian)
            "-y",
            str(output_path)
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        
        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            logging.info("Simplified audio enhancement completed")
            return output_path
        else:
            # Last resort: just copy the file
            logging.warning("Simplified enhancement failed, using raw audio")
            shutil.copy2(raw_audio_path, output_path)
            return output_path
            
    except Exception as exc:
        logging.warning("Simplified audio enhancement failed: %s", exc)
        try:
            shutil.copy2(raw_audio_path, output_path)
            return output_path
        except Exception:
            return None


def generate_audio(script: str, output_path: Path, config: Config) -> Optional[Path]:
    """Generate audio narration with fallback chain: Google Cloud TTS -> Edge-TTS -> None.
    
    All generated audio is automatically enhanced with professional studio-quality processing.
    """
    logging.info("Starting audio generation...")
    
    # Create temporary path for raw audio before enhancement
    raw_audio_path = output_path.parent / f"raw_{output_path.name}"
    
    # Try Google Cloud TTS first
    if config.use_gcloud_tts:
        logging.info("Attempting Google Cloud TTS (primary)...")
        gcloud_audio = generate_audio_with_gcloud_tts(script, raw_audio_path, config)
        if gcloud_audio:
            logging.info("Audio generation completed using Google Cloud TTS")
            # Enhance the audio to studio quality
            enhanced = enhance_audio_professional(raw_audio_path, output_path)
            # Clean up raw audio file
            try:
                if raw_audio_path.exists():
                    raw_audio_path.unlink()
            except Exception:
                pass  # Ignore cleanup errors
            if enhanced:
                return enhanced
            # If enhancement failed, try to use raw audio
            if raw_audio_path.exists():
                shutil.copy2(raw_audio_path, output_path)
                return output_path
        logging.info("Google Cloud TTS unavailable, falling back to Edge-TTS")
    else:
        logging.info("Google Cloud TTS disabled in config, using Edge-TTS")
    
    # Fallback to Edge-TTS
    logging.info("Attempting Edge-TTS (fallback)...")
    edge_audio = generate_audio_with_edge_tts(script, raw_audio_path, config)
    if edge_audio:
        logging.info("Audio generation completed using Edge-TTS")
        # Enhance the audio to studio quality
        enhanced = enhance_audio_professional(raw_audio_path, output_path)
        # Clean up raw audio file
        try:
            if raw_audio_path.exists():
                raw_audio_path.unlink()
        except Exception:
            pass  # Ignore cleanup errors
        if enhanced:
            return enhanced
        # If enhancement failed, try to use raw audio
        if raw_audio_path.exists():
            shutil.copy2(raw_audio_path, output_path)
            return output_path
    
    logging.error("All TTS methods failed - no audio generated")
    
    # Final fallback: silent video
    logging.warning("All audio generation methods failed, video will be silent")
    return None


def extract_word_timings(audio_path: Path, script: str, config: Config) -> List[WordTiming]:
    """Extract word-level timings from audio file.
    
    Uses speech recognition (whisper-timestamped) to get word timings.
    Falls back to estimated timing based on script if recognition fails.
    
    Args:
        audio_path: Path to audio file
        script: Original script text
        config: Config object
        
    Returns:
        List of WordTiming objects with word, start_time, end_time
    """
    try:
        # Try to use whisper-timestamped for accurate word-level timing
        try:
            import whisper_timestamped as whisper
            import torch
            
            logging.info("Extracting word timings using whisper-timestamped...")
            # Try tiny model first (faster), fallback to base if needed
            try:
                model = whisper.load_model("tiny", device="cpu")
            except:
                model = whisper.load_model("base", device="cpu")
            
            audio = whisper.load_audio(str(audio_path))
            result = whisper.transcribe_timestamped(
                model, 
                audio, 
                language="en",
                verbose=False
            )
            
            word_timings = []
            for segment in result.get("segments", []):
                for word_info in segment.get("words", []):
                    word_text = word_info.get("text", "").strip()
                    start_time = word_info.get("start", 0.0)
                    end_time = word_info.get("end", 0.0)
                    
                    # Skip empty words
                    if word_text:
                        word_timings.append(WordTiming(
                            word=word_text,
                            start_time=start_time,
                            end_time=end_time
                        ))
            
            if word_timings:
                logging.info("Extracted %d word timings from audio using whisper", len(word_timings))
                return word_timings
            else:
                logging.warning("Whisper returned no word timings, using fallback")
        except ImportError:
            logging.debug("whisper-timestamped not available, using fallback timing")
        except Exception as exc:
            logging.warning("Failed to extract timings with whisper: %s, using fallback", exc)
        
        # Fallback: Estimate timing based on script and audio duration with improved algorithm
        logging.info("Using improved estimated word timings based on script...")
        try:
            audio_clip = AudioFileClip(str(audio_path))
            audio_duration = audio_clip.duration
            audio_clip.close()
        except Exception:
            audio_duration = len(script.split()) * 0.5  # Estimate 0.5s per word
        
        words = script.split()
        if not words:
            return []
        
        # Improved timing estimation algorithm
        # Calculate base speaking rate (words per second)
        base_wps = len(words) / audio_duration
        
        # Estimate syllables per word (rough approximation)
        def estimate_syllables(word):
            word = word.lower()
            if len(word) <= 3:
                return 1
            word = re.sub(r'[^aeiouy]', '', word)
            syllables = len(word)
            if syllables == 0:
                return 1
            return max(1, syllables)
        
        # Calculate total "speech units" (weighted by syllables and pauses)
        total_units = 0.0
        word_units = []
        
        for i, word in enumerate(words):
            clean_word = re.sub(r'[^\w\s]', '', word)
            if not clean_word:
                word_units.append(0.0)
                continue
            
            # Base unit: syllables in word
            syllables = estimate_syllables(clean_word)
            unit = syllables * 0.15  # ~150ms per syllable
            
            # Add pause time for punctuation
            has_comma = ',' in word or ';' in word or ':' in word
            has_period = '.' in word or '!' in word or '?' in word
            
            if has_period:
                unit += 0.4  # Longer pause after sentence end
            elif has_comma:
                unit += 0.2  # Shorter pause after comma
            
            # Longer words get slightly more time
            if len(clean_word) > 6:
                unit += 0.1
            
            word_units.append(unit)
            total_units += unit
        
        # Scale units to match audio duration
        if total_units > 0:
            scale_factor = audio_duration / total_units
        else:
            scale_factor = audio_duration / len(words)
        
        # Generate word timings
        word_timings = []
        current_time = 0.0
        
        for i, word in enumerate(words):
            clean_word = re.sub(r'[^\w\s]', '', word)
            if not clean_word:
                # Still add timing for punctuation-only words
                word_timings.append(WordTiming(
                    word=word,
                    start_time=current_time,
                    end_time=current_time + 0.05
                ))
                current_time += 0.05
                continue
            
            start_time = current_time
            # Use scaled unit for duration
            word_duration = word_units[i] * scale_factor
            # Ensure minimum duration
            word_duration = max(0.15, word_duration)
            end_time = start_time + word_duration
            
            word_timings.append(WordTiming(
                word=word,
                start_time=start_time,
                end_time=end_time
            ))
            
            current_time = end_time
        
        # Adjust final timing to match audio duration exactly
        if word_timings and current_time > 0:
            final_time = word_timings[-1].end_time
            if final_time < audio_duration:
                # Stretch timings to fill audio duration
                stretch_factor = audio_duration / final_time
                for wt in word_timings:
                    wt.start_time *= stretch_factor
                    wt.end_time *= stretch_factor
            elif final_time > audio_duration:
                # Compress timings to fit audio duration
                compress_factor = audio_duration / final_time
                for wt in word_timings:
                    wt.start_time *= compress_factor
                    wt.end_time *= compress_factor
        
        logging.info("Estimated %d word timings from script (audio: %.2fs)", len(word_timings), audio_duration)
        return word_timings
        
    except Exception as exc:
        logging.error("Failed to extract word timings: %s", exc)
        return []


def group_words_into_phrases(word_timings: List[WordTiming], max_chars_per_line: int = 40) -> List[Phrase]:
    """Group words into readable caption phrases.
    
    Groups words considering:
    - Character limits per line (max_chars_per_line)
    - Natural pauses (punctuation, longer gaps)
    - Maximum 2 lines per phrase
    
    Args:
        word_timings: List of WordTiming objects
        max_chars_per_line: Maximum characters per line
        
    Returns:
        List of Phrase objects
    """
    if not word_timings:
        return []
    
    phrases = []
    current_phrase_words = []
    current_text = ""
    phrase_start_time = word_timings[0].start_time if word_timings else 0.0
    
    for i, word_timing in enumerate(word_timings):
        word = word_timing.word
        clean_word = re.sub(r'[^\w\s]', '', word)
        
        # Check if adding this word would exceed character limit
        test_text = current_text + (" " if current_text else "") + word
        test_length = len(test_text)
        
        # Check for natural break points
        is_punctuation = bool(re.search(r'[.!?,:;]', word))
        is_end_punctuation = bool(re.search(r'[.!?]', word))
        
        # Check for pause (gap > 0.3 seconds)
        has_pause = False
        if i > 0:
            gap = word_timing.start_time - word_timings[i-1].end_time
            has_pause = gap > 0.3
        
        # Start new phrase if:
        # 1. Exceeds character limit AND (has punctuation OR pause)
        # 2. Has end punctuation (period, exclamation, question mark)
        # 3. Current phrase is already long enough (> 30 chars) AND has pause
        should_break = False
        if test_length > max_chars_per_line:
            if is_punctuation or has_pause or len(current_phrase_words) > 8:
                should_break = True
        elif is_end_punctuation and len(current_phrase_words) >= 3:
            should_break = True
        elif has_pause and len(current_phrase_words) >= 5 and test_length > 30:
            should_break = True
        
        if should_break and current_phrase_words:
            # Create phrase from current words
            phrase_text = " ".join(wt.word for wt in current_phrase_words)
            phrase_end_time = current_phrase_words[-1].end_time
            
            phrases.append(Phrase(
                text=phrase_text,
                start_time=phrase_start_time,
                end_time=phrase_end_time,
                words=current_phrase_words.copy()
            ))
            
            # Start new phrase
            current_phrase_words = [word_timing]
            current_text = word
            phrase_start_time = word_timing.start_time
        else:
            # Add word to current phrase
            current_phrase_words.append(word_timing)
            current_text = test_text
    
    # Add final phrase
    if current_phrase_words:
        phrase_text = " ".join(wt.word for wt in current_phrase_words)
        phrase_end_time = current_phrase_words[-1].end_time
        
        phrases.append(Phrase(
            text=phrase_text,
            start_time=phrase_start_time,
            end_time=phrase_end_time,
            words=current_phrase_words.copy()
        ))
    
    return phrases


def create_caption_clip(phrase: Phrase, video_size: Tuple[int, int], config: Config) -> Optional[ImageClip]:
    """Create a Capcut-style caption clip with rounded background.
    
    Uses PIL to render text (no ImageMagick required).
    
    Args:
        phrase: Phrase object with text and timing
        video_size: Tuple of (width, height) for video
        config: Config object with caption settings
        
    Returns:
        CompositeVideoClip with styling, or None if creation fails
    """
    try:
        video_width, video_height = video_size
        font_size = config.caption_font_size
        fade_duration = config.caption_fade_duration
        
        # Get font path (prefer Coiny, fallback to system fonts)
        font_path = get_coiny_font_path(config)
        font = None
        if font_path and Path(font_path).exists():
            try:
                font = ImageFont.truetype(font_path, font_size)
            except Exception:
                font = None
        
        # Fallback to default bold font
        if font is None:
            try:
                # Try to load a bold system font
                font = ImageFont.truetype("arial.ttf", font_size)
            except Exception:
                try:
                    font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", font_size)
                except Exception:
                    # Final fallback to default font
                    font = ImageFont.load_default()
                    # Scale default font to approximate size
                    if hasattr(font, 'size'):
                        font = ImageFont.load_default()
        
        # Calculate caption position based on config
        if config.caption_position == "bottom":
            y_position = video_height - 250  # Position above bottom overlay
        elif config.caption_position == "top":
            y_position = 100
        else:  # center
            y_position = video_height // 2
        
        # Render text to image using PIL
        # First, measure text to determine size
        max_text_width = video_width - 100  # Leave margins
        
        # Split text into lines if needed (word wrap)
        words = phrase.text.split()
        lines = []
        current_line = ""
        
        for word in words:
            test_line = current_line + (" " if current_line else "") + word
            # Get text width using a temporary image
            test_img = Image.new('RGB', (1, 1))
            test_draw = ImageDraw.Draw(test_img)
            bbox = test_draw.textbbox((0, 0), test_line, font=font)
            text_width = bbox[2] - bbox[0]
            
            if text_width <= max_text_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word
        
        if current_line:
            lines.append(current_line)
        
        if not lines:
            lines = [phrase.text]
        
        # Calculate text dimensions
        line_heights = []
        max_line_width = 0
        for line in lines:
            test_img = Image.new('RGB', (1, 1))
            test_draw = ImageDraw.Draw(test_img)
            bbox = test_draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            line_heights.append(line_height)
            max_line_width = max(max_line_width, line_width)
        
        total_text_height = sum(line_heights) + (len(lines) - 1) * 10  # 10px spacing between lines
        
        # Add padding
        padding_x = 40
        padding_y = 20
        bg_width = int(max_line_width + padding_x * 2)
        bg_height = int(total_text_height + padding_y * 2)
        
        # Create background image with rounded corners
        bg_img = create_rounded_background(
            width=bg_width,
            height=bg_height,
            corner_radius=12,
            color=(0, 0, 0),  # Black
            opacity=0.75  # 75% opacity
        )
        
        # Draw text on background
        draw = ImageDraw.Draw(bg_img)
        text_y = padding_y
        for i, line in enumerate(lines):
            # Get text bbox for this line
            bbox = draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            
            # Center text horizontally
            text_x = (bg_width - line_width) // 2
            
            # Draw text with black outline (shadow effect)
            outline_color = (0, 0, 0)
            for adj in [(-2, -2), (-2, 2), (2, -2), (2, 2), (-2, 0), (2, 0), (0, -2), (0, 2)]:
                draw.text((text_x + adj[0], text_y + adj[1]), line, font=font, fill=outline_color)
            
            # Draw white text on top
            draw.text((text_x, text_y), line, font=font, fill='#FFFFFF')
            
            text_y += line_height + 10  # Move to next line with spacing
        
        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
            caption_img_path = tmp_file.name
            bg_img.save(caption_img_path, "PNG")
        
        # Create image clip
        phrase_duration = phrase.end_time - phrase.start_time
        caption_img_clip = ImageClip(caption_img_path).set_duration(phrase_duration)
        caption_img_clip = caption_img_clip.set_start(phrase.start_time)
        caption_img_clip = caption_img_clip.set_position(("center", y_position))
        
        # Apply fade animations
        caption_img_clip = caption_img_clip.fadein(fade_duration)
        caption_img_clip = caption_img_clip.fadeout(fade_duration)
        
        return caption_img_clip
        
    except Exception as exc:
        logging.warning("Failed to create caption clip: %s", exc, exc_info=True)
        return None




def generate_captions(audio_path: Path, script: str, video_size: Tuple[int, int], config: Config) -> List[ImageClip]:
    """Generate all caption clips for a video.
    
    Args:
        audio_path: Path to audio file
        script: Original script text
        video_size: Tuple of (width, height)
        config: Config object
        
    Returns:
        List of ImageClip objects for captions
    """
    if not config.enable_captions:
        return []
    
    logging.info("Generating captions...")
    
    # Extract word timings
    word_timings = extract_word_timings(audio_path, script, config)
    if not word_timings:
        logging.warning("No word timings extracted, skipping captions")
        return []
    
    # Group into phrases
    phrases = group_words_into_phrases(word_timings, config.caption_max_chars_per_line)
    if not phrases:
        logging.warning("No phrases created, skipping captions")
        return []
    
    logging.info("Created %d caption phrases", len(phrases))
    
    # Create caption clips in parallel for faster processing
    caption_clips = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(create_caption_clip, phrase, video_size, config): phrase for phrase in phrases}
        for future in as_completed(futures):
            clip = future.result()
            if clip:
                caption_clips.append(clip)
    
    logging.info("Generated %d caption clips", len(caption_clips))
    return caption_clips


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


def get_coiny_font_path(config: Config) -> Optional[str]:
    """Download and return path to Coiny font from Google Fonts.
    Returns the font file path, or None if download fails."""
    fonts_dir = Path(__file__).parent / "fonts"
    fonts_dir.mkdir(exist_ok=True)
    
    font_path = fonts_dir / "Coiny-Regular.ttf"
    
    # Return cached font if it exists
    if font_path.exists():
        return str(font_path)
    
    # Download Coiny font from Google Fonts
    try:
        logging.info("Downloading Coiny font from Google Fonts...")
        # Direct download link for Coiny Regular TTF
        font_url = "https://github.com/google/fonts/raw/main/ofl/coiny/Coiny-Regular.ttf"
        
        response = requests.get(font_url, timeout=30)
        response.raise_for_status()
        
        # Save font file
        with open(font_path, "wb") as f:
            f.write(response.content)
        
        logging.info("Coiny font downloaded successfully to %s", font_path)
        return str(font_path)
        
    except Exception as exc:
        logging.warning("Failed to download Coiny font: %s, falling back to Arial", exc)
        return None


def commit_and_push_video(video_path: Path, article_title: str) -> bool:
    """Commit and push video file to git repository.
    
    Returns:
        True if successful, False otherwise
    """
    if not video_path.exists():
        logging.warning("Video file does not exist, cannot commit: %s", video_path)
        return False
    
    try:
        # Start from video path and work up to find git repository
        search_path = video_path.resolve().parent
        repo_root = None
        
        # Try to find git repository root by checking parent directories
        for _ in range(10):  # Limit search to 10 levels up
            git_dir = search_path / ".git"
            if git_dir.exists():
                repo_root = search_path
                break
            if search_path == search_path.parent:  # Reached filesystem root
                break
            search_path = search_path.parent
        
        if not repo_root:
            # Fallback: try git rev-parse from current working directory
            repo_root_result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                cwd=Path.cwd()
            )
            if repo_root_result.returncode == 0:
                repo_root = Path(repo_root_result.stdout.strip())
            else:
                logging.debug("Not in a git repository, skipping commit/push")
                return False
        
        # Get relative path from repository root
        try:
            relative_video_path = video_path.resolve().relative_to(repo_root.resolve())
        except ValueError:
            # Video is outside repository, use absolute path (shouldn't happen)
            logging.warning("Video path is outside git repository: %s", video_path)
            return False
        
        # Configure git user if not already configured (for GitHub Actions)
        subprocess.run(
            ["git", "config", "user.name", "TechNewsDaily Bot"],
            capture_output=True,
            cwd=repo_root
        )
        subprocess.run(
            ["git", "config", "user.email", "technewsdaily@users.noreply.github.com"],
            capture_output=True,
            cwd=repo_root
        )
        
        # Add video file
        subprocess.run(
            ["git", "add", str(relative_video_path)],
            check=True,
            capture_output=True,
            cwd=repo_root
        )
        
        # Commit with descriptive message
        commit_message = f"Add video: {article_title[:60]}"
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            capture_output=True,
            text=True,
            cwd=repo_root
        )
        
        if commit_result.returncode == 0:
            logging.info("Committed video to git: %s", relative_video_path)
        elif "nothing to commit" in commit_result.stdout.lower() or "nothing to commit" in commit_result.stderr.lower():
            logging.debug("Video already committed, nothing to commit")
            return True
        else:
            logging.warning("Git commit failed: %s", commit_result.stderr)
            return False
        
        # Push to remote
        push_result = subprocess.run(
            ["git", "push"],
            capture_output=True,
            text=True,
            cwd=repo_root
        )
        
        if push_result.returncode == 0:
            logging.info("Pushed video to git repository")
            return True
        else:
            logging.warning("Git push failed: %s", push_result.stderr)
            return False
            
    except subprocess.CalledProcessError as exc:
        logging.warning("Git operation failed: %s", exc)
        return False
    except Exception as exc:
        logging.warning("Failed to commit/push video to git: %s", exc)
        return False


def assemble_video(article: ArticleCandidate, script: str, config: Config, video_index: int = 0) -> Path:
    # Generate unique filename based on story title and index
    # Sanitize title for filename
    safe_title = re.sub(r'[^\w\s-]', '', article.title)[:50].strip().replace(' ', '_')
    if not safe_title:
        safe_title = f"story_{video_index + 1}"
    filename = f"tech_news_{video_index + 1}_{safe_title}.mp4"
    output_path = config.output_dir / filename
    video_size = (1080, 1920)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Generate audio first to determine duration
        audio_path = tmp_path / "narration.wav"  # LINEAR16 WAV format
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

        # Prepare stock media (multiple videos and images) - fetch more to ensure we have enough
        stock_video_paths, stock_image_paths = prepare_stock_media(article, config, tmp_path, count=10)
        
        # Create video clips
        video_clips = []
        clips_to_close = []  # Track clips that need explicit closing
        
        # Cache video durations to avoid reopening files
        video_durations = {}
        
        # Helper function to get video duration (cached)
        def get_video_duration(video_path):
            if video_path not in video_durations:
                try:
                    temp_video = VideoFileClip(video_path)
                    video_durations[video_path] = temp_video.duration
                    temp_video.close()
                    # Explicitly close reader to free resources
                    if hasattr(temp_video, 'reader') and temp_video.reader:
                        try:
                            temp_video.reader.close()
                        except:
                            pass
                except:
                    video_durations[video_path] = 10.0  # Default fallback
            return video_durations[video_path]
        
        # Helper function to create video clip from a video file
        def create_video_clip(video_path, start_time, duration, fade_in=False, fade_out=False):
            """Create a video clip segment with optional fades."""
            stock_video = VideoFileClip(video_path)
            clips_to_close.append(stock_video)
            
            # Resize video to 1080x1920
            stock_video = stock_video.resize(height=1920)
            if stock_video.w > 1080:
                stock_video = stock_video.crop(x_center=stock_video.w/2, width=1080)
            elif stock_video.w < 1080:
                stock_video = stock_video.resize(width=1080)
            
            # Get segment from video (loop if needed)
            video_duration = stock_video.duration
            video_durations[video_path] = video_duration  # Cache it
            
            if start_time >= video_duration:
                start_time = start_time % video_duration
            end_time = min(start_time + duration, video_duration)
            
            video_segment = stock_video.subclip(start_time, end_time)
            
            # If we need more duration, loop the video
            if video_segment.duration < duration:
                remaining = duration - video_segment.duration
                loop_segments = [video_segment]
                loop_start = 0
                while remaining > 0:
                    loop_duration = min(remaining, video_duration)
                    loop_segment = stock_video.subclip(loop_start, loop_start + loop_duration)
                    loop_segments.append(loop_segment)
                    remaining -= loop_duration
                    loop_start = (loop_start + loop_duration) % video_duration
                if len(loop_segments) > 1:
                    video_segment = concatenate_videoclips(loop_segments)
            
            # Add fades
            if fade_in:
                video_segment = video_segment.fadein(0.5)
            if fade_out:
                video_segment = video_segment.fadeout(0.5)
            
            return video_segment
        
        # Helper function to create image clip
        def create_image_clip(image_path, duration, zoom_start=1.1, zoom_end=1.0, fade_in=False, fade_out=False):
            """Create an image clip with Ken Burns effect."""
            img_clip = ImageClip(str(image_path)).set_duration(duration)
            img_clip = img_clip.resize(lambda t: zoom_start + (zoom_end - zoom_start) * (t / duration))
            img_clip = img_clip.set_position(("center", "center"))
            if fade_in:
                img_clip = img_clip.fadein(0.5)
            if fade_out:
                img_clip = img_clip.fadeout(0.5)
            return img_clip
        
        # Try to fill duration with stock videos, looping through them as needed
        accumulated_duration = 0.0
        video_index = 0
        video_start_time = 0.0
        
        if stock_video_paths:
            try:
                while accumulated_duration < duration_seconds and len(video_clips) < 100:  # Safety limit
                    remaining = duration_seconds - accumulated_duration
                    if remaining <= 0:
                        break
                    
                    # Get next video (loop through list if needed)
                    if video_index >= len(stock_video_paths):
                        # Fetch more videos if we've used all and still need more
                        if accumulated_duration < duration_seconds * 0.5:  # Only fetch more if we're less than halfway
                            logging.info("Need more videos, fetching additional stock videos...")
                            additional_videos, _ = prepare_stock_media(article, config, tmp_path, count=5)
                            if additional_videos:
                                stock_video_paths.extend(additional_videos)
                            else:
                                break  # No more videos available
                        else:
                            # Loop back to start of video list
                            video_index = 0
                            video_start_time = 0.0
                    
                    video_path = stock_video_paths[video_index]
                    
                    try:
                        if not Path(video_path).exists() or Path(video_path).stat().st_size == 0:
                            video_index += 1
                            continue
                        
                        # Determine segment duration (try to use at least 3 seconds per clip)
                        segment_duration = min(remaining, max(3.0, remaining / max(1, len(stock_video_paths))))
                        
                        # Create clip with fades
                        fade_in = len(video_clips) > 0
                        fade_out = (accumulated_duration + segment_duration) < duration_seconds
                        
                        video_segment = create_video_clip(
                            video_path, 
                            video_start_time, 
                            segment_duration,
                            fade_in=fade_in,
                            fade_out=fade_out
                        )
                        
                        video_clips.append(video_segment)
                        accumulated_duration += segment_duration
                        video_start_time += segment_duration
                        
                        # Move to next video if we've used most of current one
                        video_duration = get_video_duration(video_path)
                        if video_start_time >= video_duration - 1.0:
                            video_index += 1
                            video_start_time = 0.0
                    except Exception as exc:
                        logging.warning("Failed to use stock video %d: %s, trying next", video_index + 1, exc)
                        video_index += 1
                        video_start_time = 0.0
                        continue
                
                # Fill remaining time with images if needed
                if accumulated_duration < duration_seconds and stock_image_paths:
                    remaining = duration_seconds - accumulated_duration
                    logging.debug("Filling remaining %.2fs with images", remaining)
                    image_index = 0
                    image_duration_accum = 0.0
                    
                    while image_duration_accum < remaining:
                        image_remaining = remaining - image_duration_accum
                        duration_per_image = min(image_remaining, max(2.0, remaining / len(stock_image_paths)))
                        
                        if image_index >= len(stock_image_paths):
                            image_index = 0  # Loop through images
                        
                        image_path = stock_image_paths[image_index]
                        try:
                            zoom_start = 1.1 if image_index % 2 == 0 else 1.0
                            zoom_end = 1.0 if image_index % 2 == 0 else 1.1
                            fade_in = len(video_clips) > 0 or image_index > 0
                            fade_out = image_duration_accum + duration_per_image < remaining
                            
                            img_clip = create_image_clip(
                                image_path,
                                duration_per_image,
                                zoom_start=zoom_start,
                                zoom_end=zoom_end,
                                fade_in=fade_in,
                                fade_out=fade_out
                            )
                            video_clips.append(img_clip)
                            image_duration_accum += duration_per_image
                            image_index += 1
                        except Exception as exc:
                            logging.debug("Failed to create clip from image %d: %s", image_index, exc)
                            image_index += 1
                            continue
                    
                    accumulated_duration += image_duration_accum
                
                if video_clips:
                    logging.info("Created %d clip(s) (%.1fs / %.1fs target)", 
                               len(video_clips), accumulated_duration, duration_seconds)
                else:
                    logging.warning("No stock videos could be used, falling back to images")
                    # Close any clips that were created before the error
                    for clip in clips_to_close:
                        try:
                            clip.close()
                        except:
                            pass
                    clips_to_close.clear()
            except Exception as exc:
                logging.warning("Failed to process stock videos: %s, falling back to images", exc)
                # Close any clips that were created before the error
                for clip in clips_to_close:
                    try:
                        clip.close()
                    except:
                        pass
                clips_to_close.clear()
        
        # Use multiple images if no videos available or videos didn't work
        if not video_clips and stock_image_paths:
            accumulated_duration = 0.0
            image_index = 0
            
            while accumulated_duration < duration_seconds:
                remaining = duration_seconds - accumulated_duration
                duration_per_image = min(remaining, max(2.0, duration_seconds / len(stock_image_paths)))
                
                if image_index >= len(stock_image_paths):
                    image_index = 0  # Loop through images
                
                image_path = stock_image_paths[image_index]
                try:
                    zoom_start = 1.1 if image_index % 2 == 0 else 1.0
                    zoom_end = 1.0 if image_index % 2 == 0 else 1.1
                    fade_in = image_index > 0
                    fade_out = accumulated_duration + duration_per_image < duration_seconds
                    
                    img_clip = create_image_clip(
                        image_path,
                        duration_per_image,
                        zoom_start=zoom_start,
                        zoom_end=zoom_end,
                        fade_in=fade_in,
                        fade_out=fade_out
                    )
                    video_clips.append(img_clip)
                    accumulated_duration += duration_per_image
                    image_index += 1
                except Exception as exc:
                    logging.debug("Failed to create clip from image %d: %s", image_index, exc)
                    image_index += 1
                    continue
            
            if not video_clips:
                # Fallback to single image
                image_path = tmp_path / "frame.jpg"
                ensure_image(image_path, article, config)
                img_clip = create_image_clip(image_path, duration_seconds, zoom_start=1.2, zoom_end=1.0)
                video_clips.append(img_clip)
        
        if not video_clips:
            # Final fallback: single placeholder image
            image_path = tmp_path / "frame.jpg"
            ensure_image(image_path, article, config)
            img_clip = create_image_clip(image_path, duration_seconds, zoom_start=1.2, zoom_end=1.0)
            video_clips.append(img_clip)
        
        # Concatenate all video clips
        if len(video_clips) > 1:
            base_video = concatenate_videoclips(video_clips, method="compose")
            # Note: concatenate_videoclips creates a new clip, original clips still need closing
        else:
            base_video = video_clips[0]
        
        # Ensure exact duration - extend if too short, trim if too long
        if base_video.duration < duration_seconds:
            # Video is too short - loop the last segment to fill
            remaining = duration_seconds - base_video.duration
            logging.warning("Video too short (%.2fs < %.2fs), extending by %.2fs", 
                       base_video.duration, duration_seconds, remaining)
            
            if remaining > 0.1:  # Only extend if meaningful duration needed
                try:
                    # Simple approach: loop the last second of the video to fill remaining time
                    last_segment_duration = min(remaining, max(1.0, base_video.duration))
                    last_segment = base_video.subclip(max(0, base_video.duration - last_segment_duration), base_video.duration)
                    
                    # Loop this segment to fill remaining time
                    num_loops = int(remaining / last_segment_duration) + 1
                    looped_segments = [last_segment] * num_loops
                    extension_clip = concatenate_videoclips(looped_segments).subclip(0, remaining)
                    
                    # Concatenate with original
                    base_video = concatenate_videoclips([base_video, extension_clip]).set_duration(duration_seconds)
                    logging.info("Extended video to %.2fs by looping last segment", duration_seconds)
                except Exception as exc:
                    logging.warning("Failed to extend video by looping, setting duration directly: %s", exc)
                    # Fallback: just set duration (will freeze on last frame)
                    base_video = base_video.set_duration(duration_seconds)
            else:
                base_video = base_video.set_duration(duration_seconds)
        elif base_video.duration > duration_seconds:
            # Video is too long - trim to exact duration
            base_video = base_video.subclip(0, duration_seconds)

        # Create subtle gradient overlay at bottom
        # This helps with readability on bright backgrounds
        overlay = ColorClip(
            size=(video_size[0], 200), 
            color=(0, 0, 0)
        ).set_opacity(0.2).set_position(("center", video_size[1] - 200)).set_duration(duration_seconds)

        # Generate captions if enabled
        caption_clips = []
        if config.enable_captions and generated_audio and generated_audio.exists():
            try:
                caption_clips = generate_captions(audio_path, script, video_size, config)
            except Exception as exc:
                logging.warning("Failed to generate captions: %s", exc)
                caption_clips = []

        # Composite base video, overlay, and captions
        clips = [base_video, overlay] + caption_clips
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
                fps=20,  # Reduced from 24 - barely noticeable, faster encoding
                codec="libx264",
                audio_codec="aac" if audio_clip else None,
                bitrate="3500k",  # Reduced from 5000k - still high quality for 1080p, faster encoding
                verbose=False,
                logger=None,
                preset="fast",  # Changed from "medium" - faster encoding with minimal quality loss
                threads=4,  # Use multiple threads for faster encoding
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
    
    # Ensure video is also saved to artifacts folder (for GitHub Actions)
    # This ensures videos are available in the artifacts folder even if OUTPUT_DIR is set elsewhere
    artifacts_dir = Path("artifacts")
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    
    # Copy video to artifacts folder if it's not already there
    artifacts_video_path = artifacts_dir / output_path.name
    if output_path.resolve() != artifacts_video_path.resolve() and output_path.exists():
        try:
            shutil.copy2(output_path, artifacts_video_path)
            logging.info("Video also saved to artifacts folder: %s", artifacts_video_path)
            output_path = artifacts_video_path  # Use artifacts path for git operations
        except Exception as exc:
            logging.warning("Failed to copy video to artifacts folder: %s", exc)
    elif output_path.resolve() == artifacts_video_path.resolve():
        logging.debug("Video already in artifacts folder: %s", output_path)
    
    # Commit and push video to git repository
    try:
        commit_and_push_video(output_path, article.title)
    except Exception as exc:
        logging.warning("Failed to commit and push video to git: %s", exc)
    
    return output_path


def main() -> None:
    setup_logging()
    setup_nltk()  # Initialize NLTK data for newspaper3k
    config = load_config()
    
    if config.ai_only_mode:
        logging.info("Running in AI-only mode (minimum %d AI keywords required)", config.min_ai_keywords)

    # Select multiple top stories (up to max_videos_per_day)
    stories = select_top_stories(DEFAULT_SOURCES, config.max_articles, config.max_videos_per_day, config)
    if not stories:
        if config.ai_only_mode:
            logging.error("Pipeline halted: no AI-related stories available")
        else:
            logging.error("Pipeline halted: no stories available")
        return
    
    logging.info("Selected %d story/stories for video generation", len(stories))
    
    # Process each story
    successful_videos = 0
    failed_videos = 0
    
    for video_index, story in enumerate(stories):
        logging.info("=" * 60)
        logging.info("Processing video %d/%d: %s", video_index + 1, len(stories), story.title[:60])
        logging.info("=" * 60)
        
        try:
            script = generate_script(story, config)
            metadata = generate_metadata(story, script)
            video_path = assemble_video(story, script, config, video_index)

            logging.info("Video generation completed successfully")
            logging.info("Video path: %s", video_path)
            logging.info("Metadata: %s", metadata)
            
            # Create thumbnail
            thumbnail_path = None
            if config.upload_to_youtube:
                try:
                    # Clean title for filename (move regex outside f-string to avoid backslash issue)
                    safe_title = re.sub(r'[^\w\s-]', '', story.title)[:30].strip().replace(' ', '_')
                    thumbnail_filename = f"thumbnail_{video_index + 1}_{safe_title}.png"
                    thumbnail_path = config.output_dir / thumbnail_filename
                    thumbnail_path = create_thumbnail(story, metadata["title"], thumbnail_path, config)
                    if thumbnail_path:
                        logging.info("Thumbnail created: %s", thumbnail_path)
                    else:
                        logging.warning("Thumbnail creation failed, continuing without thumbnail")
                except Exception as exc:
                    logging.warning("Failed to create thumbnail: %s", exc)
            
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
                        thumbnail_path,
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
            
            # Log success for this video
            logging.info("Video %d/%d completed:", video_index + 1, len(stories))
            logging.info("  File: %s", video_path)
            if youtube_video_id:
                logging.info("  YouTube: https://www.youtube.com/watch?v=%s", youtube_video_id)
            if tiktok_video_id:
                logging.info("  TikTok: %s", tiktok_video_id)
            
            # Mark story as covered (only if video was successfully created)
            try:
                save_covered_story(story, config, youtube_video_id, tiktok_video_id)
            except Exception as exc:
                logging.warning("Failed to save covered story: %s", exc)
            
            successful_videos += 1
            
        except Exception as exc:
            logging.error("Failed to process video %d/%d for story '%s': %s", 
                         video_index + 1, len(stories), story.title[:60], exc, exc_info=True)
            failed_videos += 1
            continue
    
    # Final summary
    logging.info("=" * 60)
    logging.info("Pipeline completed: %d successful, %d failed out of %d videos", 
                successful_videos, failed_videos, len(stories))
    logging.info("=" * 60)
    if successful_videos == 0:
        logging.warning("No videos were successfully created")


if __name__ == "__main__":
    main()

