2025-11-28 21:55:10,621 | INFO | Starting audio generation...
2025-11-28 21:55:10,621 | INFO | Attempting Google Cloud TTS (primary)...
2025-11-28 21:55:10,623 | INFO | ============================================================
2025-11-28 21:55:10,623 | INFO | Google Cloud TTS Configuration:
2025-11-28 21:55:10,623 | INFO |   Model: Neural2 (default)
2025-11-28 21:55:10,623 | INFO |   Voice Name: 
2025-11-28 21:55:10,623 | INFO |   Language/Locale: 
2025-11-28 21:55:10,623 | INFO |   Audio Encoding: MP3
2025-11-28 21:55:10,623 | INFO |   Speaking Rate: 1.0
2025-11-28 21:55:10,623 | INFO |   Pitch: 0.0
2025-11-28 21:55:10,623 | INFO | ============================================================
2025-11-28 21:55:10,713 | WARNING | Google Cloud TTS attempt 1/3 failed: 400 Voice name and locale cannot both be empty., retrying in 1s
2025-11-28 21:55:11,744 | WARNING | Google Cloud TTS attempt 2/3 failed: 400 Voice name and locale cannot both be empty., retrying in 2s
2025-11-28 21:55:13,773 | WARNING | Google Cloud TTS failed after 3 attempts: 400 Voice name and locale cannot both be empty.
2025-11-28 21:55:13,773 | INFO | Google Cloud TTS unavailable, falling back to Edge-TTS
2025-11-28 21:55:13,773 | INFO | Attempting Edge-TTS (fallback)...
2025-11-28 21:55:13,773 | INFO | ============================================================
2025-11-28 21:55:13,773 | INFO | Edge-TTS Configuration (Fallback):
2025-11-28 21:55:13,773 | INFO |   Provider: Microsoft Edge TTS
2025-11-28 21:55:13,773 | INFO |   Voice: 
2025-11-28 21:55:13,773 | INFO | ============================================================
2025-11-28 21:55:13,774 | WARNING | Edge-TTS audio generation failed: Invalid voice ''.
2025-11-28 21:55:13,775 | ERROR | All TTS methods failed - no audio generated
2025-11-28 21:55:13,775 | WARNING | All audio generation methods failed, video will be silent with captions