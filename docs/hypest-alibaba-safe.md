# HYPEST Alibaba veilige lijstverversing — gemeten 3 oktober 2026

## Accountcontrole

De proef is uitgevoerd op het bedoelde HYPEST-inkoopaccount. De exacte account- en tabidentifiers en de ruwe ordergegevens blijven uitsluitend in de lokale bewijsbestanden; er wordt geen bewijsdata in deze PR opgenomen.

## Resultaat

Live proef: 47 unieke orders over vijf pagina's (10/10/10/10/7), alle 46 bestaande IDs plus één nieuwe order. Account vóór en na ophaling bevestigd op account_settings. Geen betaling/ordertoevoeging/winkelwijziging. Stel HYPEST_ALIBABA_MEMBER_ID en HYPEST_ALIBABA_ACCOUNT_NAME in vanuit het lokaal geverifieerde account; zonder beide blokkeert de code. Accountgegevens blijven buiten deze openbare repository.

Bewijs en kandidaat:
- /home/ubuntu/.hermes/cache/scratch/alibaba-safe-evidence-final-20261003.json
- /home/ubuntu/.hermes/cache/scratch/alibaba-safe-candidate-final-20261003.json

## Exacte backend

De gewone browser_navigate en browser_console draaien Camofox REST op http://127.0.0.1:9377, userId agent-default, bestaande lokaal geverifieerde tab. GET /tabs zonder userId gaf [] terwijl /health activeTabs=1 gaf; GET /tabs?userId=agent-default toont de live tab. Dit is identiteitsfiltering, geen lege browser. Config cloud_provider/backend=camofox, CDP leeg, browser_backend_name()=camofox; userAgent Firefox/152.0. Skills' algemene profielnaam is niet het actuele accountbewijs: echte pagina toont Michelle.

## Gedrag en tests

Ophaler gebruikt tools.browser_tool/browser_camofox uit bestaande Hermes-runtime. Adopteert alleen bestaande tab; ontbrekende tab blokkeert vóór navigatie. Geen nieuwe browser, wachtwoord/code/mailbox/kluis-script, cookie-export of credential-file reader. Er wordt alleen naar account/listpagina's genavigeerd, de bewezen promotieclose en vaste paginaknoppen worden geklikt via browser_console. Ref-klikken meldden succes maar deden niets onder Alibaba's promotie-overlay; vaste DOM-knoppen werkten, echte paginawissel wordt steeds gecontroleerd.

Merge weigert verkeerde account, ontbrekende pagina, veranderde telling/filter/URL, dubbele ID, ontbrekende oude order, onvolledige rij/bedrag/datum. Details (items, betaling, tracking, carrier, delivery_note, onbekende velden) worden nooit vervangen door lijstsamenvattingen. Nieuwe order houdt items leeg; list_items en raw tekst tonen bron, geen verzonnen details. ID's blijven exact: historische bron heeft 16/17/18 cijfers; datums zijn zowel Engels als ISO. Live doelpad en bronpad mogen nooit uitvoerpad zijn. Kandidaten moeten nieuw zijn.

8 unit tests groen; twee volledige live proeven groen; bash -n en git diff --check groen. Live bronhash bij verificatie: 3c3fce31ac0dbcdb2438c0ef6df648e709894a7f202d39e80a820e977cb67404.

## Ondersteunde cronroute (niet geïnstalleerd/geactiveerd)

Hermes officiële cron docs en live `hermes cron create --help` bevestigen --script, --no-agent, --paused, --paused-reason, --deliver, --workdir. Eerst bewaakte review/leverroute van eigen branch, daarna scripts naar default ~/.hermes/scripts/ kopiëren. Geen hoofdmap/gatewayherstart nodig. Gebruik wrapper met nonblocking flock, uniek uitvoerpad onder default state/alibaba-safe-candidates en bestaande runtime-venv.

```sh
hermes cron create 'every 6h' --name 'HYPEST Alibaba veilige kandidaten' --script hypest-alibaba-safe-cron.sh --no-agent --deliver local --workdir /home/ubuntu --paused --paused-reason 'Read-only kandidaatroute; geen live orders-publicatie toegestaan'
```

Daarna opgeslagen exacte job teruglezen. Handmatige proef van de geïnstalleerde wrapper en kandidaat/evidence teruglezen voordat hervatten überhaupt wordt overwogen. Oude jobs 62a5a00528a8/cc06d62c834e blijven enabled=false,state=paused. Niet koppelen aan oud alibaba_agent_flow.py. Deze route levert bewust alleen kandidaten, geen automatische vervanging van live orders. Publicatie vereist aparte bestaande beslissing/poort en controle door parent; deze taak wijzigde live orders niet.

## Opgeloste proefproblemen

Systeempython miste yaml: gebruik bestaande runtimevenv, geen installatie. Ontbrekende bootstrap-identiteit: expliciet gemeten lokale endpoint/agent-default, geen .env lezen. Snapshottruncatie en succesvolle-no-op ref-klikken: vaste beperkte DOM-paginaknoppen en paginawisselcontrole. Historical ID-lengte en ISO-datum: parser aangepast op echte bron. Gbrain meldde twee gearchiveerde bronnen (code-hypest-theme-v2/code-hypest-e2e); buiten deze Alibaba-reparatie, niet gewijzigd.
