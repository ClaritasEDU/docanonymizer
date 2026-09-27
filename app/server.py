"""Flask server. Single-page app + JSON API.

Routes:
  GET  /                              - the single-page UI
  GET  /api/health                    - server liveness
  GET  /api/endpoints                 - list configured LLM endpoints
  POST /api/endpoints                 - add or update endpoint
  DEL  /api/endpoints/<id>            - delete endpoint
  POST /api/endpoints/<id>/select     - set as active
  POST /api/endpoints/health          - health-check all (or one)
  POST /api/endpoints/check-local     - dry-run is_local_url for an entered URL

  GET  /api/github                    - list GitHub connections (token redacted)
  POST /api/github                    - add or update connection
  DEL  /api/github/<id>               - delete connection
  POST /api/github/<id>/test          - test PAT/repo access
  POST /api/github/push               - push a session output

  POST /api/anonymize/upload          - multipart upload, kicks off detection
  GET  /api/anonymize/<sid>/status    - poll detection progress + preview data
  POST /api/anonymize/<sid>/confirm   - confirm preview, run scrub + verify
  GET  /api/anonymize/<sid>/results   - get final results / verification status
  GET  /api/anonymize/<sid>/download/file  - download scrubbed file
  GET  /api/anonymize/<sid>/download/key   - download key.json
  POST /api/anonymize/<sid>/cancel    - discard session, delete upload

  POST /api/unanonymize               - restore a file (AI output or anonymized file)
  POST /api/unanonymize/text          - restore pasted text (AI output)
  GET  /api/unanonymize/download/<n>  - download restored file

  GET  /api/keys                       - list keys for the unanonymize picker
  POST /api/keys/import                - copy an external key file into /keys
  GET  /api/log/tail                   - tail the log file (last N lines)
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from . import endpoints as endpoints_mod
from . import github_mgr
from . import pipeline
from . import unanonymize as unan
from .config import (
    KEYS_DIR,
    LOG_FILE,
    MAX_UPLOAD_BYTES,
    OUTPUT_DIR,
    PORT,
    STATIC_DIR,
    TEMPLATES_DIR,
    UPLOADS_DIR,
)
from .extractors import OUTPUT_NOTES, SUPPORTED, libreoffice_available
from .key_files import import_key_file, list_recent, load_key_file, saved_key_path
from .logging_setup import get_logger
from .mapper import TAG_ORDER, VALID_TAGS
from .replacer import LiteralReplacer
from .restorer import KeyConflictError, KeyIndex, build_index, restore_text

log = get_logger("server")


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    endpoints_mod.ensure_initialized()
    pipeline.purge_stale_uploads()
    _log_startup()

    # -----------------------------------------------------------------------
    # UI
    # -----------------------------------------------------------------------
    @app.get("/")
    def index():
        return render_template(
            "index.html",
            supported_extensions=sorted(SUPPORTED),
            output_notes=OUTPUT_NOTES,
            valid_tags=TAG_ORDER,
            libreoffice=libreoffice_available(),
        )

    # -----------------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    # -----------------------------------------------------------------------
    # Endpoint manager
    # -----------------------------------------------------------------------
    @app.get("/api/endpoints")
    def ep_list():
        return jsonify({
            "endpoints": endpoints_mod.list_endpoints(),
            "active": (endpoints_mod.get_active() or {}).get("id"),
        })

    @app.post("/api/endpoints")
    def ep_save():
        payload = request.get_json(force=True, silent=True) or {}
        try:
            saved = endpoints_mod.add_or_update(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"endpoint": saved})

    @app.delete("/api/endpoints/<eid>")
    def ep_delete(eid: str):
        ok = endpoints_mod.delete(eid)
        return ("", 204) if ok else (jsonify({"error": "not found"}), 404)

    @app.post("/api/endpoints/<eid>/select")
    def ep_select(eid: str):
        if not endpoints_mod.set_last_used(eid):
            return jsonify({"error": "not found"}), 404
        return jsonify({"active": eid})

    @app.post("/api/endpoints/health")
    def ep_health():
        # The open page polls this every 30s - a cheap moment to discard
        # sessions abandoned at the preview.
        pipeline.expire_abandoned()
        payload = request.get_json(silent=True) or {}
        eid = payload.get("id")
        if eid:
            ep = endpoints_mod.get(eid)
            if not ep:
                return jsonify({"error": "not found"}), 404
            return jsonify({"results": [endpoints_mod.health_check(ep)]})
        return jsonify({"results": endpoints_mod.health_check_all()})

    @app.post("/api/endpoints/check-local")
    def ep_check_local():
        payload = request.get_json(force=True, silent=True) or {}
        url = payload.get("base_url", "")
        return jsonify({"local": endpoints_mod.is_local_url(url)})

    # -----------------------------------------------------------------------
    # GitHub manager
    # -----------------------------------------------------------------------
    @app.get("/api/github")
    def gh_list():
        return jsonify({"connections": github_mgr.list_connections()})

    @app.post("/api/github")
    def gh_save():
        payload = request.get_json(force=True, silent=True) or {}
        try:
            saved = github_mgr.add_or_update(payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"connection": saved})

    @app.delete("/api/github/<cid>")
    def gh_delete(cid: str):
        ok = github_mgr.delete(cid)
        return ("", 204) if ok else (jsonify({"error": "not found"}), 404)

    @app.post("/api/github/<cid>/test")
    def gh_test(cid: str):
        conn = github_mgr.get(cid)
        if not conn:
            return jsonify({"error": "not found"}), 404
        return jsonify(github_mgr.test_connection(conn))

    @app.post("/api/github/push")
    def gh_push():
        payload = request.get_json(force=True, silent=True) or {}
        sid = payload.get("session_id")
        cid = payload.get("connection_id")
        sess = pipeline.get_session(sid) if sid else None
        if not sess or not sess.output_path:
            return jsonify({"error": "session not found or has no output"}), 404
        if not sess.verify_result or not sess.verify_result.get("passed"):
            return jsonify({"error": "verification not passed - push blocked"}), 400
        conn = github_mgr.get(cid) if cid else None
        if not conn:
            return jsonify({"error": "github connection not found"}), 404
        result = github_mgr.push_file(
            conn,
            sess.output_path,
            dest_path=payload.get("path"),
            branch=payload.get("branch"),
            commit_message=payload.get("commit_message"),
        )
        if result.get("status") != "ok":
            return jsonify(result), 502
        return jsonify(result)

    # -----------------------------------------------------------------------
    # Anonymize pipeline
    # -----------------------------------------------------------------------
    @app.post("/api/anonymize/upload")
    def anon_upload():
        pipeline.expire_abandoned()
        if "file" not in request.files:
            return jsonify({"error": "no file"}), 400
        f = request.files["file"]
        if not f.filename:
            return jsonify({"error": "empty filename"}), 400
        safe = secure_filename(f.filename)
        suffix = Path(safe).suffix.lower().lstrip(".")
        if suffix not in SUPPORTED:
            return jsonify({"error": f"unsupported file type: .{suffix}"}), 400

        # Tags selection - JSON body field is a string; falls back to "ALL".
        tags_raw = request.form.get("tags", "")
        if tags_raw and tags_raw != "ALL":
            allowed = [t.strip().upper() for t in tags_raw.split(",") if t.strip()]
            allowed = [t for t in allowed if t in VALID_TAGS]
            if not allowed:
                return jsonify({"error": "no valid tags supplied"}), 400
        else:
            allowed = sorted(VALID_TAGS)

        ep_id = request.form.get("endpoint_id")
        endpoint = endpoints_mod.get(ep_id) if ep_id else None

        # Operator-supplied custom terms: one per line, optional "TAG: " prefix
        # (default PERSON). Guaranteed catches on top of LLM detection.
        custom_terms: list[tuple[str, str]] = []
        for line in (request.form.get("custom_terms") or "").splitlines():
            s = line.strip()
            if not s:
                continue
            head, sep, rest = s.partition(":")
            if sep and head.strip().upper() in VALID_TAGS and rest.strip():
                custom_terms.append((head.strip().upper(), rest.strip()))
            else:
                custom_terms.append(("PERSON", s))

        # Save upload to /uploads with a session-prefixed name.
        sid_holder: dict = {}
        upload_path = UPLOADS_DIR / safe
        # If the user uploads two files in a row with the same name, suffix.
        if upload_path.exists():
            stem = upload_path.stem
            ext = upload_path.suffix
            i = 1
            while upload_path.exists():
                upload_path = UPLOADS_DIR / f"{stem}_{i}{ext}"
                i += 1
        f.save(upload_path)
        # Read the size now: the worker may finish (or fail and delete the
        # upload) before this request builds its response.
        upload_size = upload_path.stat().st_size
        log.info("upload received: name=%s size=%d type=%s",
                 upload_path.name, upload_size, suffix)

        sess = pipeline.new_session(upload_path, safe, allowed, endpoint=endpoint,
                                    custom_terms=custom_terms)
        sid_holder["sid"] = sess.id

        # Run extract + detect in a worker so the HTTP call returns immediately.
        threading.Thread(
            target=pipeline.run_extract_and_detect, args=(sess,), daemon=True,
        ).start()

        return jsonify({
            "session_id": sess.id,
            "filename": safe,
            "suffix": suffix,
            "output_ext": OUTPUT_NOTES[suffix][0],
            "output_note": OUTPUT_NOTES[suffix][1],
            "size": upload_size,
        })

    @app.get("/api/anonymize/<sid>/status")
    def anon_status(sid: str):
        sess = pipeline.get_session(sid)
        if not sess:
            return jsonify({"error": "session not found"}), 404
        body = {
            "session_id": sess.id,
            "detection_complete": sess.detection_complete,
            "progress": sess.detection_progress,
            "error": sess.error,
        }
        if sess.detection_complete and sess.registry is not None:
            body["preview"] = _preview_payload(sess)
        return jsonify(body)

    @app.post("/api/anonymize/<sid>/confirm")
    def anon_confirm(sid: str):
        sess = pipeline.get_session(sid)
        if not sess:
            return jsonify({"error": "session not found"}), 404
        if not sess.detection_complete:
            return jsonify({"error": "detection not complete"}), 409
        payload = request.get_json(silent=True) or {}
        deselected = [d for d in (payload.get("deselected") or []) if isinstance(d, str)]
        types = [t for t in (payload.get("deselected_types") or [])
                 if isinstance(t, str) and t in VALID_TAGS]
        threading.Thread(
            target=pipeline.confirm_and_scrub, args=(sess, deselected, types), daemon=True,
        ).start()
        return jsonify({"session_id": sid, "started": True})

    @app.get("/api/anonymize/<sid>/results")
    def anon_results(sid: str):
        sess = pipeline.get_session(sid)
        if not sess:
            return jsonify({"error": "session not found"}), 404
        return jsonify({
            "session_id": sid,
            "scrub_steps": sess.scrub_steps,
            "verify_result": sess.verify_result,
            "formula_warnings": sess.formula_warnings,
            "output_filename": sess.output_path.name if sess.output_path else None,
            "key_filename": sess.key_path.name if sess.key_path else None,
            "counts": sess.registry.counts_per_type() if sess.registry else {},
            "totals": {
                "entities": sess.registry.total_entities() if sess.registry else 0,
                "replacements": sess.registry.total_replacements() if sess.registry else 0,
            },
            "error": sess.error,
        })

    @app.get("/api/anonymize/<sid>/download/file")
    def anon_dl_file(sid: str):
        sess = pipeline.get_session(sid)
        if not sess or not sess.output_path:
            abort(404)
        # Verification gate first: a quarantined (deleted) failed output still
        # reads as "blocked", not "not found".
        if not sess.verify_result or not sess.verify_result.get("passed"):
            abort(403)
        if not sess.output_path.exists():
            abort(404)
        return send_file(sess.output_path, as_attachment=True)

    @app.get("/api/anonymize/<sid>/text")
    def anon_text(sid: str):
        """Return the scrubbed text for on-screen copy/paste.

        Verification gate applies here too - text is not released until
        the post-scrub verification pass succeeds (PRD 5.10).
        """
        sess = pipeline.get_session(sid)
        if not sess or not sess.output_path:
            abort(404)
        if not sess.verify_result or not sess.verify_result.get("passed"):
            abort(403)
        if not sess.output_path.exists():
            abort(404)
        try:
            text = pipeline.anonymized_text(sess)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({
            "text": text,
            "char_count": len(text),
            "filename": sess.output_path.name,
        })

    @app.get("/api/anonymize/<sid>/download/key")
    def anon_dl_key(sid: str):
        sess = pipeline.get_session(sid)
        if not sess or not sess.key_path:
            abort(404)
        if not sess.verify_result or not sess.verify_result.get("passed"):
            abort(403)
        if not sess.key_path.exists():
            abort(404)
        return send_file(sess.key_path, as_attachment=True)

    @app.post("/api/anonymize/<sid>/cancel")
    def anon_cancel(sid: str):
        pipeline.discard_session(sid)
        return jsonify({"discarded": sid})

    # -----------------------------------------------------------------------
    # Unanonymize
    # -----------------------------------------------------------------------
    def _unique_upload(name: str) -> Path:
        path = UPLOADS_DIR / name
        stem, ext = path.stem, path.suffix
        i = 1
        while path.exists():
            path = UPLOADS_DIR / f"{stem}_{i}{ext}"
            i += 1
        return path

    def _selected_keys(names, extra: Optional[tuple[str, dict]] = None) -> KeyIndex:
        """Build the restore index from saved key names (+ an uploaded key)."""
        if isinstance(names, str):
            try:
                names = json.loads(names) if names.strip() else []
            except ValueError:
                raise ValueError("keys must be a JSON list of key file names")
        if not isinstance(names, list):
            raise ValueError("keys must be a list of key file names")
        pairs: list[tuple[str, dict]] = []
        for name in names:
            path = saved_key_path(name)
            if path is None:
                raise ValueError(f"key not found: {name}")
            pairs.append((path.name, load_key_file(path)))
        if extra is not None:
            pairs.append(extra)
        if not pairs:
            raise ValueError("select at least one key")
        return build_index(pairs)

    @app.post("/api/unanonymize")
    def unanon():
        """Restore a file. Keys come from `keys` (JSON list of saved key
        names) and/or an uploaded `key` file."""
        if "file" not in request.files:
            return jsonify({"error": "need a file to restore"}), 400
        in_f = request.files["file"]
        if not in_f.filename:
            return jsonify({"error": "empty filename"}), 400
        safe_in = secure_filename(in_f.filename)
        suffix = Path(safe_in).suffix.lower().lstrip(".")
        if suffix not in SUPPORTED:
            return jsonify({"error": f"unsupported file type: .{suffix}"}), 400

        upload_path = _unique_upload(safe_in)
        in_f.save(upload_path)
        key_path: Optional[Path] = None
        in_k = request.files.get("key")
        if in_k is not None and in_k.filename:
            key_path = _unique_upload(secure_filename(in_k.filename) or "key.json")
            in_k.save(key_path)

        try:
            extra = None
            if key_path is not None:
                extra = (key_path.name, load_key_file(key_path))
            index = _selected_keys(request.form.get("keys", ""), extra)
            log.info("unanonymize start: file=%s keys=%d ids=%d",
                     upload_path.name, len(index.key_names), index.size)
            result = unan.restore_file(upload_path, index, display_name=safe_in)
        except KeyConflictError as exc:
            log.error("unanonymize refused: key conflict")
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            log.error("unanonymize failed: %s", exc)
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            log.exception("unanonymize failed: %s", type(exc).__name__)
            return jsonify({"error": str(exc)}), 500
        finally:
            for tmp in (upload_path, key_path):
                try:
                    if tmp is not None and tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

        out_path = result.output_path
        return jsonify({
            "output_filename": out_path.name,
            "download_url": f"/api/unanonymize/download/{out_path.name}",
            "report": result.report.as_dict(),
            "text": result.text,
        })

    @app.post("/api/unanonymize/text")
    def unanon_text():
        """Restore pasted text (an AI tool's answer). Nothing is written to disk."""
        payload = request.get_json(force=True, silent=True)
        if not isinstance(payload, dict):
            payload = {}
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            log.warning("text restore refused: empty input")
            return jsonify({"error": "paste some text to restore"}), 400
        try:
            index = _selected_keys(payload.get("keys") or [])
            restored, report = restore_text(text, index)
        except KeyConflictError as exc:
            log.error("text restore refused: key conflict")
            return jsonify({"error": str(exc)}), 409
        except ValueError as exc:
            log.warning("text restore refused: %s", exc)
            return jsonify({"error": str(exc)}), 400
        return jsonify({"text": restored, "report": report.as_dict()})

    @app.get("/api/unanonymize/download/<name>")
    def unanon_download(name: str):
        safe = secure_filename(name)
        target = OUTPUT_DIR / safe
        if not target.exists() or OUTPUT_DIR not in target.parents:
            abort(404)
        return send_file(target, as_attachment=True)

    # -----------------------------------------------------------------------
    # Keys + log tail
    # -----------------------------------------------------------------------
    @app.get("/api/keys")
    def keys_index():
        return jsonify({"keys": list_recent()})

    @app.post("/api/keys/import")
    def keys_import():
        in_k = request.files.get("key")
        if in_k is None or not in_k.filename:
            return jsonify({"error": "no key file"}), 400
        tmp = _unique_upload(secure_filename(in_k.filename) or "key.json")
        in_k.save(tmp)
        try:
            dest = import_key_file(tmp, in_k.filename)
        except (ValueError, OSError) as exc:
            log.warning("key import refused: %s", type(exc).__name__)
            return jsonify({"error": f"not a valid key file: {exc}"}), 400
        finally:
            tmp.unlink(missing_ok=True)
        return jsonify({"imported": dest.name})

    @app.get("/api/log/tail")
    def log_tail():
        n = int(request.args.get("n", "100"))
        n = max(1, min(n, 500))
        if not LOG_FILE.exists():
            return jsonify({"lines": []})
        # Tail the file. For local logs this is fast enough.
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-n:]
        return jsonify({"lines": [ln.rstrip("\n") for ln in tail]})

    return app


def _preview_payload(sess) -> dict:
    """Build the preview body for the UI - first 10,000 chars + spans.

    Built once and cached on the session: the UI polls status repeatedly and
    the registry does not change until confirm.
    """
    if sess.registry is None or sess.extract is None:
        return {}
    if sess.preview is not None:
        return sess.preview
    text = sess.extract.text
    truncated = False
    head = text
    if len(head) > 10_000:
        head = head[:10_000]
        truncated = True
    # Same single-pass engine the scrubber uses, so the preview shows exactly
    # what will be written (non-overlapping, longest match wins - M2).
    spans = [
        {"start": s0, "end": e0, "original": head[s0:e0], "placeholder": ph}
        for s0, e0, ph in LiteralReplacer(sess.registry.as_replacement_map()).find(head)
    ]
    sess.preview = {
        "text_head": head,
        "char_count": len(text),
        "truncated": truncated,
        "spans": spans,
        "counts": sess.registry.counts_per_type(),
        "entities": sess.registry.total_entities(),
        "replacements": sess.registry.total_replacements(),
        "kept_amounts": len(sess.registry.skipped_amounts),
    }
    return sess.preview


def _log_startup() -> None:
    eps = endpoints_mod.list_endpoints()
    active = endpoints_mod.get_active() or {}
    gh_count = len(github_mgr.list_connections())
    log.info(
        "startup: port=%d endpoints=%d active=%s github_connections=%d",
        PORT, len(eps), active.get("id"), gh_count,
    )


# Convenience entrypoint (`python -m app.server`). Not built at import time -
# run.py creates the app, and a second instance here doubled startup logging.
if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=PORT, debug=False)
