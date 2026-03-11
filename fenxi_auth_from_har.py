from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

from extra_auth import _collect_auth_data, inspect_fenxi_token


class FenxiAuthRefreshError(RuntimeError):
    """Raised when fenxi auth refresh from HAR fails."""


def _load_existing(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FenxiAuthRefreshError(f"读取认证文件失败（非JSON）：{path}") from exc
    if not isinstance(data, dict):
        raise FenxiAuthRefreshError(f"认证文件结构异常：{path}")
    return data


def refresh_fenxi_auth_from_hars(
    *,
    fenxi_hars: Sequence[Path],
    extra_auth_file: Path,
    output: Path,
) -> Dict[str, Any]:
    paths = [Path(p) for p in fenxi_hars]
    if not paths:
        raise FenxiAuthRefreshError("请提供至少一个 fenxi HAR 文件。")
    for har in paths:
        if not har.exists():
            raise FenxiAuthRefreshError(f"HAR 文件不存在：{har}")

    existing = _load_existing(extra_auth_file)
    fenxi_block = _collect_auth_data(paths, "fenxi")
    diag = inspect_fenxi_token(fenxi_block, warn_threshold_hours=6.0)
    if not bool(diag.get("present")):
        raise FenxiAuthRefreshError("HAR 中未提取到 fenxi e_token，请确认 HAR 来自已登录状态。")

    payload: Dict[str, Any] = dict(existing)
    payload["fenxi"] = fenxi_block
    payload.setdefault("505", {"cookies": {}, "headers": {}, "token": "", "bootstrap_url_template": ""})
    payload.setdefault("pc_web", {"cookies": {}, "headers": {}, "token": "", "bootstrap_url_template": ""})

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    updated_meta = dict(meta)
    updated_meta.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "fenxi_har_refresh",
            "fenxi_hars": [str(p) for p in paths],
        }
    )
    payload["meta"] = updated_meta

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "output_path": str(output),
        "fenxi_present": bool(diag.get("present")),
        "fenxi_usable": bool(diag.get("usable")),
        "fenxi_reason": str(diag.get("reason") or ""),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 fenxi HAR 刷新 extra_auth.json 的 fenxi 登录态（仅更新 fenxi，不覆盖 pc/505）")
    parser.add_argument("--fenxi-har", action="append", default=[], help="fenxi HAR 文件路径，可传多次")
    parser.add_argument("--extra-auth-file", type=Path, default=Path("extra_auth.json"), help="已有 extra_auth.json")
    parser.add_argument("--output", type=Path, default=Path("extra_auth.json"), help="输出路径")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = refresh_fenxi_auth_from_hars(
        fenxi_hars=[Path(p) for p in (args.fenxi_har or [])],
        extra_auth_file=args.extra_auth_file,
        output=args.output,
    )
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
