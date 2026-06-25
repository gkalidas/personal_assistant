"""
Live news TV channel lineup for the dashboard's multi-channel grid.

Each entry is a 24/7 live YouTube news stream embedded via the IFrame API so the
frontend can mute/unmute, pause, and rewind within YouTube's live DVR buffer.

Embeds use the documented `embed/live_stream?channel=<channel_id>` URL, which
always resolves to the channel's *current* live broadcast — so the list never
needs a daily video-id refresh. If a channel ever goes dark, set `video_id` to
pin a specific stream, or update `channel_id` (these are stable YouTube IDs).
"""

from __future__ import annotations

# category is purely a UI label; lang helps the frontend group/badge tiles.
# To change the lineup, edit this list — the frontend reads it from /api/news/channels.
LIVE_NEWS_CHANNELS: list[dict] = [
    {
        "id":         "aljazeera",
        "name":       "Al Jazeera English",
        "channel_id": "UCNye-wNBqNL5ZzHSJj3l8Bg",
        "lang":       "en",
        "category":   "global",
    },
    {
        "id":         "dwnews",
        "name":       "DW News",
        "channel_id": "UCknLrEdhRCp1aegoMqRaCZg",
        "lang":       "en",
        "category":   "global",
    },
    {
        "id":         "france24",
        "name":       "FRANCE 24 English",
        "channel_id": "UCQfwfsi5VrQ8yKZ-UWmAEFg",
        "lang":       "en",
        "category":   "global",
    },
    {
        "id":         "skynews",
        "name":       "Sky News",
        "channel_id": "UCoMdktPbSTixAyNGwb-UYkQ",
        "lang":       "en",
        "category":   "global",
    },
    {
        "id":         "bloomberg",
        "name":       "Bloomberg Television",
        "channel_id": "UCIALMKvObZNtJ6AmdCLP7Lg",
        "lang":       "en",
        "category":   "global",
    },
    {
        "id":         "abcnews",
        "name":       "ABC News (US)",
        "channel_id": "UCBi2mrWuNuyYy4gbM6fU18Q",
        "lang":       "en",
        "category":   "global",
    },
    {
        "id":         "nbcnews",
        "name":       "NBC News",
        "channel_id": "UCeY0bbntWzzVIaj2z3QigXg",
        "lang":       "en",
        "category":   "global",
    },
    {
        "id":         "trtworld",
        "name":       "TRT World",
        "channel_id": "UC7fWeaHhqgM4Ry-RMpM2YYw",
        "lang":       "en",
        "category":   "global",
    },
    {
        "id":         "cna",
        "name":       "CNA (Channel NewsAsia)",
        "channel_id": "UC4p_I9eiRewn2KoU-nawrDg",
        "lang":       "en",
        "category":   "global",
    },
]
# Adding a channel: grab its channel_id from the channel's YouTube page source
# (search "channelId") and append an entry above. If a channel blocks live-stream
# embedding, pin a specific "video_id" instead of channel_id.


def embed_url(channel: dict) -> str:
    """Build the IFrame-API-enabled live embed URL for one channel.

    Prefers a pinned `video_id` when present, otherwise uses the channel's
    auto-resolving live stream. All players start muted (browser autoplay rule).
    """
    base = "https://www.youtube.com/embed"
    params = "enablejsapi=1&autoplay=1&mute=1&playsinline=1&rel=0"
    if channel.get("video_id"):
        return f"{base}/{channel['video_id']}?{params}"
    return f"{base}/live_stream?channel={channel['channel_id']}&{params}"


def channels_payload() -> dict:
    """Return the channel lineup plus ready-to-use embed URLs for the frontend."""
    return {
        "channels": [
            {**c, "embed_url": embed_url(c)}
            for c in LIVE_NEWS_CHANNELS
        ]
    }
