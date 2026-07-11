# EnergyMaster TODO

## Lage complexiteit, hoge waarde

- [ ] **Aansluitlimiet als LP-constraint**: max import/export per slot als lineaire bound in de solver, niet alleen als dispatch-clamp zoals nu in [distribution.py:67](custom_components/energymaster/distribution.py:67). Relevant voor capaciteitstarief, netcongestie en §14a. Beste prijs/kwaliteit.
- [ ] **Generieke schakelbare last**: geparametriseerde on/off-last (vermogen, draaiduur, tijdvenster) voor zwembadpomp, hottub, 2e boiler, vrieskist. Hergebruikt de bestaande appliance-scheduling van wasmachine/droger/vaatwasser.
- [ ] **Extra prijsprovider (EasyEnergy of ENTSO-E)**: drop-in via het bestaande plugin-patroon in [dynamicprices/](custom_components/energymaster/solver/dynamicprices). EasyEnergy is populair in NL.
- [ ] **Auto-SoC uit bestaande HA-autosensor**: SoC en bereik lezen die HA-integraties (VW, Tesla, etc.) al leveren, geen eigen merk-API's bouwen. Maakt EV-planning nauwkeuriger.

## Medium complexiteit, waardevol

- [ ] **SG-Ready WP-uitgang**: solver-beslissing mappen naar 2 HA-switches voor de 4 standen (blokkeer/normaal/aanbeveling/boost). Lastige deel: continue huis-als-batterij model naar 4 grove standen, plus de max-2u-blokkade regel.
- [ ] **Zonnepanelen kunnen uitschakelen (PV-curtailment)**: PV-inverter/strings uit kunnen zetten of afregelen, zodat we bij negatieve of zeer lage prijzen niet terugleveren. Nu is er wel een PV-forecast ([forecast.py](custom_components/energymaster/solver/forecast.py)) maar geen sturing op de panelen. De solver moet de curtailment-keuze meenemen in de optimalisatie.

## EV / laadsturing

- [ ] **Max load per fase instelbaar voor de lader**: per fase een instelbare maximale laadstroom/vermogen, als constraint in de solver en in de dispatch. Lage complexiteit, sluit aan op de bestaande per-fase modellering.
- [ ] **Terugschakelen naar 1 fase**: optie om de lader van 3f naar 1f te laten schakelen voor laag laden op zonneoverschot. Let op: raakt de `current_slot`-logica van de EV en vraagt een binaire fasekeuze in de LP, dus medium-hoge complexiteit.
- [ ] **Dynamisch laden op zonneopbrengst**: laadvermogen volgt het PV-overschot in realtime. Koppelt de PV-forecast/actuele opbrengst aan de laaddispatch, samen met de fasekeuze en de max-load-per-fase hierboven.
