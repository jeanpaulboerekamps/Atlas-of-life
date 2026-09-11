Atlas of Life — interactief prototype

Bestanden
- atlas_of_life_interactief.html : zelfstandige webapp, geen externe libraries nodig.

Belangrijk voor iPad
De ChatGPT-bestandsviewer voert JavaScript niet uit. Daardoor zie je daar wel de statische kaart,
maar werken pannen, pinch-zoom, zoeken en zoomlagen niet.

Voor echte interactie moet dit HTML-bestand via een normale webserver geopend worden.
Voorbeelden:
- GitHub Pages
- Netlify / Cloudflare Pages
- lokale webserver op een computer op hetzelfde netwerk

Interactie
- slepen: pannen
- pinch / muiswiel: zoomen
- tik/klik op taxon: focus
- zoekveld + Enter: spring naar taxon
- Wereld: terug naar overzicht
- Waarnemingen: aparte laag aan/uit

Architectuur
De taxonomische kaart is statische SVG, dus blijft zichtbaar zonder JavaScript.
JavaScript voegt alleen interactie en semantische zoom toe.
Waarnemingen vormen een aparte laag en zijn niet onderdeel van de taxonomische hiërarchie.
