# Twikey Outreach - LinkedIn helper (Chrome-extensie)

Dit is de "tussenweg" tussen de handmatige LinkedIn-tracker in het dashboard
en volledige automatisering (die we bewust niet bouwen — zie hieronder). Deze
extensie bespaart tikwerk en logt automatisch, maar **verstuurt of klikt
niets namens jou op LinkedIn**.

## Wat het wel en niet doet

Op elke LinkedIn-profielpagina verschijnt rechtsonder een klein paneel dat:

- de naam van het profiel automatisch overneemt (gewoon door de zichtbare
  paginatekst te lezen, zoals jij dat ook zou doen);
- een template van jouw keuze invult met die naam en toont in een
  voorbeeldveld;
- die tekst met één klik naar je klembord kopieert, zodat je hem zelf kunt
  plakken in LinkedIn's eigen bericht- of connectieverzoek-veld;
- een outreach-actie logt naar je Twikey Sales Platform dashboard **nadat
  jij aangeeft dat je iets hebt verstuurd** — dezelfde data die je nu ook al
  handmatig op de LinkedIn-tab kunt invoeren, alleen sneller.

Het klikt dus nooit zelf op "Verbinden" of "Versturen" binnen LinkedIn, en
haalt geen lijsten van profielen of connecties op. Dat is een bewuste keuze:
tools die dat wél doen (zoals Dux-Soup, We-Connect, Phantombuster en
vergelijkbaar) besturen een ingelogde LinkedIn-sessie alsof een mens klikt.
Dat schendt LinkedIn's gebruiksvoorwaarden, LinkedIn detecteert dit patroon
actief, en het risico van een (permanente) accountblokkering ligt bij jouw
eigen LinkedIn-account. Deze extensie blijft daarom aan de veilige kant van
die grens: jij blijft degene die op LinkedIn zelf klikt en verstuurt.

## Installeren (niet in de Chrome Web Store — "unpacked" laden)

1. Open Chrome en ga naar `chrome://extensions`.
2. Zet rechtsboven **Ontwikkelaarsmodus** aan.
3. Klik op **Uitgepakte extensie laden**.
4. Selecteer deze map (`browser-extension/`).
5. Het Twikey-icoontje verschijnt in je Chrome-werkbalk.

## Instellen

Klik op het extensie-icoon in de werkbalk. De templates en de log komen nu
uit hetzelfde multi-tenant account-systeem als het dashboard, dus:

1. Log eerst in met je e-mailadres en wachtwoord van het Twikey-dashboard
   (zelfde account, apart sessietoken — dit token staat los van een
   ingelogde sessie in `dashboard.html`).
2. Controleer daaronder de **Backend API-URL** — die staat standaard op je
   Render-backend (`https://twikey-platform-backend.onrender.com`). Pas dit
   alleen aan als je backend-service een andere naam/URL heeft.

Zonder in te loggen krijg je op de LinkedIn-profielpagina de melding "Je bent
niet ingelogd" in plaats van templates.

## Gebruiken

1. Ga naar een LinkedIn-profielpagina (`linkedin.com/in/...`).
2. Het paneel rechtsonder toont de gedetecteerde naam (pas aan indien nodig)
   en een leeg "Bedrijf"-veld (vul dit zelf in voor `{{company}}` in het
   template — laat je dit leeg, dan valt de tekst automatisch terug op
   "your company" zodat de zin grammaticaal klopt).
3. Kies een template — het voorbeeldveld toont direct de ingevulde tekst.
4. Klik op **Kopieer naar klembord**.
5. Ga naar LinkedIn's eigen "Verbinden"- of berichtvenster, plak de tekst,
   en verstuur het **zelf** zoals je altijd al deed.
6. Klik op de bijpassende log-knop ("Log: verzoek verstuurd", etc.) — dit
   verschijnt meteen op de LinkedIn-tab van je dashboard.

## Onderhoud / beperkingen

- De naam- en bedrijfsdetectie is een best-effort gok op basis van de
  zichtbare pagina-tekst (LinkedIn's HTML-structuur is niet gedocumenteerd
  en verandert af en toe) — controleer/pas de velden altijd even na, zeker
  het "Bedrijf"-veld.
- Als LinkedIn een grote layout-wijziging doorvoert, kan de naamdetectie
  stoppen met werken; de rest van de extensie (templates, kopiëren, loggen)
  blijft dan gewoon werken, je vult de naam dan zelf in.
- Als je de host_permissions in `manifest.json` aanpast (bijv. een andere
  backend-URL toevoegt), moet je de extensie opnieuw laden via
  `chrome://extensions` → het herlaad-icoontje bij deze extensie.
