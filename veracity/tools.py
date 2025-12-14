from urllib.parse import quote_plus

from flask import url_for


def generate_external_tools(
    image_url: str | None, *, analysis_id: str | None = None
) -> list[dict]:
    """Generate links to external tools for reverse image search.

    If we have a public URL, we generate direct search links.
    If it's a local file upload, we link to the search engines' upload pages or
    fall back to our own cached asset URL when available.
    """
    if not image_url and analysis_id:
        image_url = url_for(
            "main.serve_analysis_image", analysis_id=analysis_id, _external=True
        )

    tools: list[dict] = []
    links: list[dict] = []

    # Google Reverse Search / Lens
    if image_url:
        encoded_url = quote_plus(image_url)
        link = f"https://lens.google.com/upload?url={encoded_url}"
    else:
        link = "https://images.google.com/"

    links.append({"label": "Google", "url": link})

    # Bing Visual Search
    if image_url:
        encoded_url = quote_plus(image_url)
        link = (
            "https://www.bing.com/images/search?view=detailv2&iss=sbi&form="
            "SBIHMP&q=imgurl:"
            f"{encoded_url}"
        )
    else:
        link = "https://www.bing.com/visualsearch"

    links.append({"label": "Bing", "url": link})

    # TinEye (only when we have a public URL)
    if image_url:
        encoded_url = quote_plus(image_url)
        link = f"https://tineye.com/search?url={encoded_url}"
        links.append({"label": "TinEye", "url": link})

    tools.append({"name": "Reverse Image Search", "links": links})

    return tools
