#!/usr/bin/env python3
"""Build the curated Podcast Addict RSS feed using Apple episode metadata.

Apple's podcast episode lookup exposes the audio URL supplied by the publisher,
which avoids host-specific RSS blocking while keeping the enclosure on the
original podcast host/CDN.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
import xml.etree.ElementTree as ET

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
SELF_URL = "https://raw.githubusercontent.com/RobNVermeer/ai-listening-briefing/main/ai-briefing.xml"
REPO_URL = "https://github.com/RobNVermeer/ai-listening-briefing"

ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)


def norm(value: str) -> str:
    value = html.unescape(value or "")
    value = unicodedata.normalize("NFKD", value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Rob-AI-Listening-Briefing/1.0",
            "Accept": "application/json, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def apple_episodes(show_id: int) -> list[dict]:
    query = urllib.parse.urlencode(
        {
            "id": str(show_id),
            "country": "US",
            "media": "podcast",
            "entity": "podcastEpisode",
            "limit": "200",
        }
    )
    payload = fetch_json(f"https://itunes.apple.com/lookup?{query}")
    return [
        result
        for result in payload.get("results", [])
        if result.get("wrapperType") == "podcastEpisode"
        or result.get("kind") == "podcast-episode"
    ]


def find_episode(episodes: list[dict], wanted_title: str) -> dict:
    wanted = norm(wanted_title)
    exact = [ep for ep in episodes if norm(ep.get("trackName", "")) == wanted]
    if exact:
        return exact[0]

    partial = [
        ep
        for ep in episodes
        if wanted in norm(ep.get("trackName", ""))
        or norm(ep.get("trackName", "")) in wanted
    ]
    if len(partial) == 1:
        return partial[0]

    recent = "\n".join(f"  - {ep.get('trackName', '')}" for ep in episodes[:30])
    raise RuntimeError(
        f"Could not uniquely match episode title: {wanted_title!r}. Recent Apple titles:\n{recent}"
    )


def duration_from_ms(ms: int | None) -> str:
    if not ms:
        return ""
    seconds = int(ms) // 1000
    hours, rem = divmod(seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def stable_guid(show_id: int, ep: dict) -> str:
    original = str(ep.get("episodeGuid") or ep.get("trackId") or ep.get("episodeUrl") or ep.get("trackName"))
    digest = hashlib.sha256(f"{show_id}|{original}".encode("utf-8")).hexdigest()[:24]
    return f"rob-ai-briefing-{digest}"


def build() -> None:
    config = json.loads(Path("selection.json").read_text(encoding="utf-8"))
    editions = config.get("editions", [])
    if not editions:
        raise RuntimeError("selection.json has no editions")

    show_cache: dict[int, list[dict]] = {}
    resolved: list[dict] = []

    for edition in editions:
        selections = sorted(edition["selections"], key=lambda x: x["order"])
        core_count = sum(1 for s in selections if s.get("slot", "core") == "core")
        edition_base = datetime.fromisoformat(edition["date"]).replace(
            hour=18, minute=0, second=0, tzinfo=timezone.utc
        )

        for sel in selections:
            show_id = int(sel["apple_show_id"])
            if show_id not in show_cache:
                show_cache[show_id] = apple_episodes(show_id)
                if not show_cache[show_id]:
                    raise RuntimeError(f"Apple returned no episodes for show {show_id}")

            ep = find_episode(show_cache[show_id], sel["episode_title"])
            audio_url = ep.get("episodeUrl")
            if not audio_url:
                raise RuntimeError(f"Apple returned no audio URL for {ep.get('trackName')!r}")

            slot = sel.get("slot", "core")
            order = int(sel["order"])
            prefix = "[BONUS]" if slot == "bonus" else f"[{order}/{core_count}]"
            resolved.append(
                {
                    "edition_date": edition["date"],
                    "edition_title": edition["title"],
                    "synthetic_date": edition_base - timedelta(minutes=order - 1),
                    "prefix": prefix,
                    "show_title": ep.get("collectionName") or "Podcast",
                    "episode_title": ep.get("trackName") or sel["episode_title"],
                    "why": sel["why"],
                    "original_date": ep.get("releaseDate") or "Original publication date unavailable",
                    "link": ep.get("trackViewUrl") or ep.get("collectionViewUrl") or "",
                    "guid": stable_guid(show_id, ep),
                    "enclosure": {
                        "url": audio_url,
                        "length": "0",
                        "type": "audio/mpeg",
                    },
                    "duration": duration_from_ms(ep.get("trackTimeMillis")),
                }
            )

    resolved.sort(key=lambda x: x["synthetic_date"], reverse=True)

    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = config["feed_title"]
    ET.SubElement(channel, "link").text = REPO_URL
    ET.SubElement(
        channel,
        f"{{{ATOM}}}link",
        {"href": SELF_URL, "rel": "self", "type": "application/rss+xml"},
    )
    ET.SubElement(channel, "description").text = config["feed_description"]
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "generator").text = "Rob AI Listening Briefing feed builder"
    ET.SubElement(channel, "ttl").text = "60"
    newest = max(x["synthetic_date"] for x in resolved)
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(newest)
    ET.SubElement(channel, f"{{{ITUNES}}}author").text = "AI Listening Briefing"
    ET.SubElement(channel, f"{{{ITUNES}}}summary").text = (
        "A serious weekly AI listening programme curated from original podcast publishers."
    )
    ET.SubElement(channel, f"{{{ITUNES}}}explicit").text = "false"
    ET.SubElement(channel, f"{{{ITUNES}}}block").text = "yes"
    ET.SubElement(channel, f"{{{ITUNES}}}type").text = "episodic"

    for ep in resolved:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = (
            f"{ep['prefix']} {ep['episode_title']} — {ep['show_title']}"
        )
        ET.SubElement(item, "description").text = (
            f"Edition: {ep['edition_date']} — {ep['edition_title']}\n\n"
            f"Why it matters: {ep['why']}\n\n"
            f"Source: {ep['show_title']}\n"
            f"Original publication: {ep['original_date']}"
        )
        if ep["link"]:
            ET.SubElement(item, "link").text = ep["link"]
        ET.SubElement(item, "pubDate").text = format_datetime(ep["synthetic_date"])
        guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
        guid.text = ep["guid"]
        ET.SubElement(item, "enclosure", ep["enclosure"])
        if ep["duration"]:
            ET.SubElement(item, f"{{{ITUNES}}}duration").text = ep["duration"]
        ET.SubElement(item, f"{{{ITUNES}}}explicit").text = "false"

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write("ai-briefing.xml", encoding="utf-8", xml_declaration=True)
    print(f"Built ai-briefing.xml with {len(resolved)} curated episodes.")
    for ep in resolved:
        print(f"{ep['prefix']} {ep['episode_title']} — {ep['show_title']}")


if __name__ == "__main__":
    try:
        build()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
