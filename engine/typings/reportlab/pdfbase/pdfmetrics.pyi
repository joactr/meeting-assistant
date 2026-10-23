from reportlab.pdfbase.ttfonts import TTFont

# Application PDF exports register the bundled TrueType Unicode font.
def registerFont(font: TTFont) -> None: ...
