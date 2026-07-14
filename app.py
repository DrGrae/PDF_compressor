"""Streamlit front-end for converting images to compressed PDFs."""
import io
import zipfile
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

import streamlit as st
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from receipt_to_pdf import (
    SUPPORTED_EXTS_DEFAULT,
    open_image_normalized,
    try_compress,
)

# Compression defaults (mirroring the CLI script)
MAX_SIZE_BYTES = 1_000_000
INITIAL_QUALITY = 85
MIN_QUALITY = 35
QUALITY_STEP = 5
INITIAL_LONG_EDGE = 2000
MIN_LONG_EDGE = 800
PDF_DPI = 150
BACKGROUND_RGB = (255, 255, 255)

SUPPORTED_IMAGE_TYPES = sorted({ext.lower() for ext in SUPPORTED_EXTS_DEFAULT})
SUPPORTED_IMAGE_TYPES_SET = set(SUPPORTED_IMAGE_TYPES)
SUPPORTED_UPLOAD_TYPES = sorted({*SUPPORTED_IMAGE_TYPES, "pdf"})


def sanitize_component(raw_value: str, fallback: str) -> str:
    """Keep only filesystem-friendly characters."""
    sanitized = raw_value.strip().replace(" ", "_")
    allowed = {"-", "_"}
    sanitized = "".join(ch for ch in sanitized if ch.isalnum() or ch in allowed)
    sanitized = sanitized.strip("._-")
    return sanitized or fallback


def unique_filename(used_names: set, filename: str) -> str:
    """Dedupe a filename in-memory against names already used in this batch."""
    if filename not in used_names:
        used_names.add(filename)
        return filename
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        candidate = f"{stem}_{counter}{suffix}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        counter += 1


def load_uploaded_file(uploaded_file) -> Tuple[str, bytes]:
    data = uploaded_file.getvalue()
    if not data:
        raise ValueError("File was empty.")
    return uploaded_file.name, data


def convert_files(uploaded_files: Iterable, merge_mode: bool, prefix: str):
    sanitized_prefix = sanitize_component(prefix, "PDF")
    used_names: set = set()

    results: List[dict] = []
    errors: List[str] = []

    uploads: List[dict] = []
    for uploaded_file in uploaded_files:
        try:
            name, data = load_uploaded_file(uploaded_file)
        except ValueError as exc:
            errors.append(f"{uploaded_file.name}: {exc}")
            continue

        ext = Path(name).suffix.lower().lstrip(".")
        if ext == "pdf":
            uploads.append({"type": "pdf", "name": name, "data": data})
        elif ext in SUPPORTED_IMAGE_TYPES_SET:
            uploads.append({"type": "image", "name": name, "data": data})
        else:
            errors.append(f"{uploaded_file.name}: Unsupported file type")

    def convert_image_group(group: Sequence[dict]):
        pil_images = []
        valid_names: List[str] = []
        for item in group:
            try:
                image = open_image_normalized(
                    io.BytesIO(item["data"]), bg_rgb=BACKGROUND_RGB, grayscale=False
                )
            except Exception as exc:  # Pillow raises many specific exceptions
                errors.append(f"{item['name']}: {exc}")
                continue

            pil_images.append(image)
            valid_names.append(item["name"])

        if not pil_images:
            return None

        try:
            pdf_bytes, final_q, final_edge, iterations = try_compress(
                pil_images,
                max_size_bytes=MAX_SIZE_BYTES,
                initial_quality=INITIAL_QUALITY,
                min_quality=MIN_QUALITY,
                initial_long_edge=INITIAL_LONG_EDGE,
                min_long_edge=MIN_LONG_EDGE,
                quality_step=QUALITY_STEP,
                pdf_dpi=PDF_DPI,
            )
        finally:
            for image in pil_images:
                image.close()

        return {
            "bytes": pdf_bytes,
            "iterations": iterations,
            "quality": final_q,
            "long_edge": final_edge,
            "source_names": valid_names,
        }

    def validate_pdf_bytes(name: str, data: bytes) -> bool:
        try:
            PdfReader(io.BytesIO(data), strict=False)
        except (PdfReadError, Exception) as exc:
            errors.append(f"{name}: {exc}")
            return False
        return True

    if merge_mode:
        segments: List[dict] = []
        current_group: List[dict] = []

        for item in uploads:
            if item["type"] == "image":
                current_group.append(item)
                continue

            if current_group:
                group_result = convert_image_group(current_group)
                if group_result:
                    segments.append(group_result)
                current_group = []

            if validate_pdf_bytes(item["name"], item["data"]):
                segments.append(
                    {
                        "bytes": item["data"],
                        "iterations": None,
                        "quality": None,
                        "long_edge": None,
                        "source_names": [item["name"]],
                    }
                )

        if current_group:
            group_result = convert_image_group(current_group)
            if group_result:
                segments.append(group_result)

        if not segments:
            raise ValueError("No valid files were uploaded.")

        writer = PdfWriter()
        merged_source_names: List[str] = []
        for segment in segments:
            try:
                reader = PdfReader(io.BytesIO(segment["bytes"]), strict=False)
            except (PdfReadError, Exception) as exc:
                errors.append(
                    f"{', '.join(segment['source_names'])}: {exc}"
                )
                continue

            for page in reader.pages:
                writer.add_page(page)
            merged_source_names.extend(segment["source_names"])

        if not writer.pages:
            raise ValueError("No valid files were uploaded.")

        buffer = io.BytesIO()
        writer.write(buffer)
        merged_bytes = buffer.getvalue()

        pdf_name = unique_filename(used_names, f"{sanitized_prefix}_merged.pdf")

        metrics_segment = (
            segments[0]
            if len(segments) == 1 and segments[0]["quality"] is not None
            else None
        )

        results.append(
            {
                "name": pdf_name,
                "bytes": merged_bytes,
                "iterations": metrics_segment["iterations"] if metrics_segment else None,
                "quality": metrics_segment["quality"] if metrics_segment else None,
                "long_edge": metrics_segment["long_edge"] if metrics_segment else None,
                "source_names": merged_source_names,
            }
        )
    else:
        for index, item in enumerate(uploads, start=1):
            if item["type"] == "pdf":
                if not validate_pdf_bytes(item["name"], item["data"]):
                    continue

                base_stem = sanitize_component(Path(item["name"]).stem, f"document_{index}")
                pdf_name = unique_filename(used_names, f"{sanitized_prefix}_{base_stem}.pdf")
                results.append(
                    {
                        "name": pdf_name,
                        "bytes": item["data"],
                        "iterations": None,
                        "quality": None,
                        "long_edge": None,
                        "source_names": [item["name"]],
                    }
                )
                continue

            try:
                image = open_image_normalized(
                    io.BytesIO(item["data"]), bg_rgb=BACKGROUND_RGB, grayscale=False
                )
            except Exception as exc:
                errors.append(f"{item['name']}: {exc}")
                continue

            try:
                pdf_bytes, final_q, final_edge, iterations = try_compress(
                    [image],
                    max_size_bytes=MAX_SIZE_BYTES,
                    initial_quality=INITIAL_QUALITY,
                    min_quality=MIN_QUALITY,
                    initial_long_edge=INITIAL_LONG_EDGE,
                    min_long_edge=MIN_LONG_EDGE,
                    quality_step=QUALITY_STEP,
                    pdf_dpi=PDF_DPI,
                )
            finally:
                image.close()

            base_stem = sanitize_component(Path(item["name"]).stem, f"image_{index}")
            pdf_name = unique_filename(used_names, f"{sanitized_prefix}_{base_stem}.pdf")
            results.append(
                {
                    "name": pdf_name,
                    "bytes": pdf_bytes,
                    "iterations": iterations,
                    "quality": final_q,
                    "long_edge": final_edge,
                    "source_names": [item["name"]],
                }
            )

    return sanitized_prefix, results, errors


def build_zip_bytes(conversions: List[dict]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in conversions:
            zf.writestr(item["name"], item["bytes"])
    return buffer.getvalue()


st.set_page_config(page_title="Image → PDF Compressor", page_icon="🧾")
st.title("Image → PDF Compressor")
st.write(
    "Convert individual images or merge several images and PDFs into compressed PDF files below 1 MB. "
    "Download the results below to upload them to your expense or record-keeping systems."
)

prefix_input = st.text_input("PDF file name prefix", value="Receipts")
merge_mode = st.toggle("Merge all uploads into a single PDF", value=False)

st.write(
    "Upload scans, photos, or PDFs in any of these formats: "
    + ", ".join(SUPPORTED_UPLOAD_TYPES)
)
uploaded_files = st.file_uploader(
    "Drag & drop your files here", type=SUPPORTED_UPLOAD_TYPES, accept_multiple_files=True
)

convert_clicked = st.button("Convert to PDF")

if convert_clicked:
    if not uploaded_files:
        st.error("Please upload at least one file.")
        st.session_state.pop("conversion_result", None)
    else:
        try:
            prefix_used, conversions, issues = convert_files(uploaded_files, merge_mode, prefix_input)
        except RuntimeError as exc:
            st.error(f"Compression failed: {exc}")
            st.session_state.pop("conversion_result", None)
        except Exception as exc:
            st.error(f"Unable to process the uploaded files: {exc}")
            st.session_state.pop("conversion_result", None)
        else:
            st.session_state["conversion_result"] = {
                "prefix_used": prefix_used,
                "prefix_input": prefix_input,
                "conversions": conversions,
                "issues": issues,
            }

result = st.session_state.get("conversion_result")
if result:
    prefix_used = result["prefix_used"]
    conversions = result["conversions"]
    issues = result["issues"]

    if conversions:
        st.success(
            f"Created {len(conversions)} PDF file{'s' if len(conversions) != 1 else ''}."
        )
        if prefix_used != sanitize_component(result["prefix_input"], "PDF"):
            st.info(f"The prefix was normalised to `{prefix_used}` for filenames.")

        if len(conversions) > 1:
            st.download_button(
                label=f"Download all {len(conversions)} as ZIP",
                data=build_zip_bytes(conversions),
                file_name=f"{prefix_used}_all.zip",
                mime="application/zip",
                key="download-all-zip",
            )

        for item in conversions:
            size_kb = len(item["bytes"]) / 1024.0
            metrics = [f"{size_kb:.1f} KB"]
            if item.get("quality") is not None:
                metrics.append(f"quality {item['quality']}")
            if item.get("long_edge") is not None:
                metrics.append(f"long edge {item['long_edge']} px")
            description = (
                f"{', '.join(item['source_names'])} → **{item['name']}** "
                f"({', '.join(metrics)})"
            )
            st.markdown(description)
            st.download_button(
                label=f"Download {item['name']}",
                data=item["bytes"],
                file_name=item["name"],
                mime="application/pdf",
                key=f"download-{item['name']}"
            )
    else:
        st.warning("No files were converted. Check the warnings below for details.")

    if issues:
        st.warning(
            "Some files could not be processed:\n" + "\n".join(f"- {msg}" for msg in issues)
        )

st.caption(
    "This tool uses the same compression pipeline as the original CLI script, with JPEG quality "
    "tuning and down-scaling to stay under 1 MB per PDF."
)
