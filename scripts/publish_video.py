"""Publish a finished OpenMontage video to YouTube and/or Instagram.

Reads the title, description, tags, hashtags and chapters that `export_bundle`
wrote to `<export>/metadata/metadata.json`, so after a pipeline run:

    # Preview what would be posted (no network, nothing published):
    python scripts/publish_video.py projects/my-video/exports --youtube --instagram

    # Actually publish (YouTube defaults to private):
    python scripts/publish_video.py projects/my-video/exports --youtube --visibility public --yes
    python scripts/publish_video.py projects/my-video/exports --instagram --yes

    # Schedule a YouTube premiere:
    python scripts/publish_video.py projects/my-video/exports --youtube \
        --publish-at 2026-11-01T15:00:00Z --yes

Instagram has no scheduling API; to schedule a Reel, run the --instagram
command from cron / Task Scheduler at the time you want it to go out.

The publish_log for each platform is written next to the bundle as
`publish_log.<platform>.json`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_bundle(export_dir: Path) -> tuple[Path, dict, Path | None]:
    meta_path = export_dir / "metadata" / "metadata.json"
    if not meta_path.is_file():
        raise SystemExit(f"No metadata at {meta_path}. Run export_bundle first.")
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    videos = sorted((export_dir / "video").glob("output.*"))
    if not videos:
        raise SystemExit(f"No video/output.* in {export_dir}.")
    thumbs = sorted(p for p in (export_dir / "thumbnails").glob("thumbnail.*"))
    return videos[0], metadata, (thumbs[0] if thumbs else None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export_dir", type=Path, help="Export bundle directory (contains video/ and metadata/).")
    parser.add_argument("--youtube", action="store_true")
    parser.add_argument("--instagram", action="store_true")
    parser.add_argument("--visibility", choices=["public", "private", "unlisted"], default="private",
                        help="YouTube visibility (default private).")
    parser.add_argument("--publish-at", help="YouTube scheduled release time, ISO-8601 (e.g. 2026-11-01T15:00:00Z).")
    parser.add_argument("--category-id", default="22", help="YouTube category id.")
    parser.add_argument("--caption", help="Instagram caption override.")
    parser.add_argument("--video-url", help="Public URL Instagram can fetch instead of uploading the file.")
    parser.add_argument("--yes", action="store_true", help="Actually publish. Without it, only prints a preview.")
    args = parser.parse_args(argv)

    if not (args.youtube or args.instagram):
        parser.error("choose at least one of --youtube / --instagram")

    from tools.tool_registry import ToolRegistry
    from tools.publishers.instagram_reels import InstagramReels
    from tools.publishers.youtube_upload import YouTubeUpload

    ToolRegistry._load_dotenv()
    video, metadata, thumb = _load_bundle(args.export_dir)

    jobs = []
    if args.youtube:
        inputs = {
            "video_path": str(video),
            "title": metadata.get("title", ""),
            "description": metadata.get("description", ""),
            "tags": metadata.get("tags", []),
            "hashtags": metadata.get("hashtags", []),
            "chapters": metadata.get("chapters", []),
            "visibility": args.visibility,
            "category_id": args.category_id,
            "confirm_publish": args.yes,
        }
        if args.publish_at:
            inputs["publish_at"] = args.publish_at
        if thumb and thumb.suffix.lower() in (".jpg", ".jpeg", ".png"):
            inputs["thumbnail_path"] = str(thumb)
        jobs.append(("youtube", YouTubeUpload(), inputs))
    if args.instagram:
        inputs = {
            "video_path": str(video),
            "title": metadata.get("title", ""),
            "description": metadata.get("description", ""),
            "hashtags": metadata.get("hashtags", []),
            "confirm_publish": args.yes,
        }
        if args.caption:
            inputs["caption"] = args.caption
        if args.video_url:
            inputs["video_url"] = args.video_url
        jobs.append(("instagram", InstagramReels(), inputs))

    exit_code = 0
    for platform, tool, inputs in jobs:
        result = tool.execute(inputs)
        if not result.success:
            print(f"[{platform}] FAILED: {result.error}", file=sys.stderr)
            exit_code = 1
        log = result.data.get("publish_log")
        if log:
            out = args.export_dir / f"publish_log.{platform}.json"
            out.write_text(json.dumps(log, indent=2), encoding="utf-8")
        if not result.success:
            continue
        if args.yes:
            print(f"[{platform}] published: {result.data.get('url')}")
            for warning in result.data.get("warnings", []):
                print(f"[{platform}] warning: {warning}")
        else:
            print(f"[{platform}] preview (not published; add --yes to publish):")
            print(json.dumps(result.data.get("request"), indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
