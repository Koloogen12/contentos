"""Visual rendering for carousels and single-post covers.

The carousel rendering pipeline turns a `FormatNode.data` payload — already
shaped by `carousel_creator` (slides[] + summary + cta) — into a set of
1080×1350 JPEG slides hosted on S3 with public HTTPS URLs.

Pipeline (orchestrated by `carousel.render_carousel_for_node`):

1. **Cover image**: a separate AI image (gpt-image-1) is generated for
   slide 1, using a prompt derived from the talking point + cover title.
   This is the only paid step per render (≈$0.04 at gpt-image-1 standard).
2. **HTML templates**: each non-cover slide is rendered into a single-page
   HTML document using the `editorial_dark` template. The cover slide is
   rendered with a different template that overlays the title on the
   generated image.
3. **Playwright screenshot**: a long-running Chromium instance (one per
   render run, not per slide) sets viewport to 1080×1350 at deviceScaleFactor=3
   and screenshots the body as JPEG.
4. **Pillow optimise**: JPEG bytes are passed through Pillow to strip
   metadata and re-encode at quality=88.
5. **S3 upload**: each slide is `save_public_bytes`-ed under
   `renders/carousel/{node_id}/{render_id}/{N}.jpg` with public-read ACL.

The render result (list of URLs + metadata) is written back into
`node.data["rendered_slides"]` so the frontend can display the images
directly without a second fetch.
"""
