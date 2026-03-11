from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

from extra_auth import _collect_auth_data


class PCAuthRefreshError(RuntimeError):
    """Raised when pc_web auth refresh from HAR fails."""


def _load_existing(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PCAuthRefreshError(f"读取认证文件失败（非JSON）：{path}") from exc
    if not isinstance(data, dict):
        raise PCAuthRefreshError(f"认证文件结构异常：{path}")
    return data


def refresh_pc_auth_from_hars(
    *,
    pc_hars: Sequence[Path],
    extra_auth_file: Path,
    output: Path,
) -> Dict[str, Any]:
    paths = [Path(p) for p in pc_hars]
    if not paths:
        raise PCAuthRefreshError("请提供至少一个 PC HAR 文件。")
    for har in paths:
        if not har.exists():
            raise PCAuthRefreshError(f"HAR 文件不存在：{har}")

    existing = _load_existing(extra_auth_file)
    pc_block = _collect_auth_data(paths, "pc_web")
    headers = pc_block.get("headers") if isinstance(pc_block.get("headers"), dict) else {}
    cookies = pc_block.get("cookies") if isinstance(pc_block.get("cookies"), dict) else {}
    has_bearer = bool(str(headers.get("Bearer") or "").strip())
    has_authorization = bool(str(headers.get("Authorization") or "").strip())
    has_admin_token = bool(str(cookies.get("Admin-Token") or "").strip())
    if not (has_bearer or has_authorization or has_admin_token):
        raise PCAuthRefreshError(
            "HAR 中未提取到可用 PC 鉴权（缺少 Bearer/Authorization/Admin-Token），请确认 HAR 来自已登录状态。"
        )

    payload: Dict[str, Any] = dict(existing)
    payload["pc_web"] = pc_block
    payload.setdefault("fenxi", {"cookies": {}, "headers": {}, "token": "", "bootstrap_url_template": ""})
    payload.setdefault("505", {"cookies": {}, "headers": {}, "token": "", "bootstrap_url_template": ""})

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    updated_meta = dict(meta)
    updated_meta.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "pc_har_refresh",
            "pc_hars": [str(p) for p in paths],
        }
    )
    payload["meta"] = updated_meta

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reason = "pc_web 鉴权已更新"
    if not (has_bearer or has_authorization):
        reason = "仅提取到 Admin-Token（建议补抓包含 Bearer 的 HAR）"
    return {
        "output_path": str(output),
        "pc_has_bearer": bool(has_bearer or has_authorization),
        "pc_has_admin_token": has_admin_token,
        "pc_reason": reason,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 PC HAR 刷新 extra_auth.json 的 pc_web 登录态（仅更新 pc_web，不覆盖 fenxi/505）")
    parser.add_argument("--pc-har", action="append", default=[], help="PC HAR 文件路径，可传多次")
    parser.add_argument("--extra-auth-file", type=Path, default=Path("extra_auth.json"), help="已有 extra_auth.json")
    parser.add_argument("--output", type=Path, default=Path("extra_auth.json"), help="输出路径")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = refresh_pc_auth_from_hars(
        pc_hars=[Path(p) for p in (args.pc_har or [])],
        extra_auth_file=args.extra_auth_file,
        output=args.output,
    )
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
