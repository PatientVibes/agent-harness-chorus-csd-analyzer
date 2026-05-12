"""Pipeline orchestration: CSD files -> ZIP with XML + JSON + manifest."""
import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from chorus_forms.preview.renderer import render_preview

logger = logging.getLogger(__name__)


def convert_files(
    input_paths: list[Path],
    work_dir: Path,
    ai_results: dict[str, dict] | None = None,
) -> bytes:
    from chorus_forms.csd.parser import parse_csd_file
    from chorus_forms.csd.adapter import csd_to_user_screen
    from chorus_forms.core.xml_builder import build_user_screen
    from chorus_forms.uxb.builder import csd_to_uxb, to_design_model

    xml_dir = work_dir / "xml"
    json_dir = work_dir / "json"
    uxb_dir = work_dir / "uxb"
    preview_dir = work_dir / "preview"
    xml_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)
    uxb_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    forms = []
    errors = []

    for path in input_paths:
        try:
            form = parse_csd_file(path)
            if form.meta.csd_version == 0 and form.meta.form_title is None:
                raise ValueError(f"File does not appear to be a valid CSD file (no version header found)")
            forms.append(form)
            json_path = json_dir / f"{path.stem}.json"
            json_path.write_text(form.model_dump_json(by_alias=True, indent=2), encoding="utf-8")
            user_screen_model = csd_to_user_screen(form)
            envelope = build_user_screen(user_screen_model)
            xml_bytes = etree.tostring(envelope, pretty_print=True, xml_declaration=True, encoding="UTF-8")
            xml_path = xml_dir / f"{path.stem}.xml"
            xml_path.write_bytes(xml_bytes)
            # UXB JSON
            try:
                uxb_doc = csd_to_uxb(form)
                uxb_model = to_design_model(uxb_doc, form_type=form.meta.form_type)
                uxb_path = uxb_dir / f"{path.stem}.json"
                uxb_path.write_text(
                    json.dumps(uxb_model.model_dump(exclude_none=True), indent=2),
                    encoding="utf-8",
                )
            except Exception as uxb_err:
                logger.warning("UXB conversion failed for %s: %s", path.name, uxb_err)
            # HTML Preview
            try:
                preview_html = render_preview(user_screen_model)
                preview_path = preview_dir / f"{path.stem}_preview.html"
                preview_path.write_text(preview_html, encoding="utf-8")
            except Exception as prev_err:
                logger.warning("Preview generation failed for %s: %s", path.name, prev_err)
            logger.info("Converted %s: %d fields, %d groups", path.name, len(form.fields), len(form.groups))
        except Exception as e:
            logger.error("Failed to convert %s: %s", path.name, e)
            errors.append({"file": path.name, "error": str(e)})

    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "totalForms": len(forms),
        "totalErrors": len(errors),
        "forms": [
            {
                "fileName": f.meta.file_name,
                "formTitle": f.meta.form_title,
                "formType": f.meta.form_type,
                "numPages": f.meta.num_pages,
                "totalControls": f.meta.total_controls,
                "inputFields": f.meta.input_fields,
                "groups": len(f.groups),
                "warnings": f.warnings,
            }
            for f in forms
        ],
        "errors": errors,
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for xml_file in sorted(xml_dir.glob("*.xml")):
            zf.write(xml_file, f"xml/{xml_file.name}")
        for json_file in sorted(json_dir.glob("*.json")):
            zf.write(json_file, f"json/{json_file.name}")
        for uxb_file in sorted(uxb_dir.glob("*.json")):
            zf.write(uxb_file, f"uxb/{uxb_file.name}")
        for preview_file in sorted(preview_dir.glob("*.html")):
            zf.write(preview_file, f"preview/{preview_file.name}")
        zf.writestr("_manifest.json", json.dumps(manifest, indent=2))
        if ai_results:
            zf.writestr("_ai_analysis.json", json.dumps(ai_results, indent=2))

    return buf.getvalue()
