# Image to PDF Compressor

Convert images into compressed PDF files using either the original command-line script or a new Streamlit web interface.

## Quick Start
1. **Install dependencies** (ideally inside a virtual environment):
   ```bash
   python -m streamlit run app.py
   ```
2. **Run the Streamlit interface**:
   ```bash
   streamlit run app.py
   ```
3. **Use the app**:
   - Enter the prefix to apply to every exported PDF.
   - Upload one or more images via drag-and-drop.
   - Toggle *Merge all uploads into a single PDF* if you want one combined document (images and/or existing PDFs).
   - Click **Convert to PDF**. Converted files stay in memory for the session and are offered as direct downloads.

## Features
- Keeps every PDF under 1 MB by matching the CLI compression settings (quality tuning and down-scaling).
- Supports the same wide range of image formats as the original script, including HEIC/HEIF when `pillow-heif` is available, and accepts pre-existing PDF files for merging.
- Automatically prefixes each output filename, deduplicating names within a batch.
- Provides both merged multi-page PDFs and per-image conversions.

## Command-Line Script
The original `receipt_to_pdf.py` script remains available if you prefer terminal workflows. Run `python receipt_to_pdf.py --help` for its usage information.
