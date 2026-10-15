from pathlib import Path
from playwright.sync_api import sync_playwright

here = Path(__file__).resolve().parent
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    pg.goto((here / "manual.html").as_uri())
    pg.pdf(path=str(here / "PC_Agent_How_It_Works.pdf"), format="A4", print_background=True,
           margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
           display_header_footer=True, header_template="<span></span>",
           footer_template='<div style="font-size:8px;width:100%;text-align:center;color:#888">'
                           '<span class="pageNumber"></span> / <span class="totalPages"></span></div>')
    b.close()
print("ok")
