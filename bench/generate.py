"""Seeded synthetic leads with known ground truth.

Every lead carries `_entity` (which real company it belongs to) and `_labels` (its true
industry + signals), so dedupe and classification can be scored, not eyeballed.
Same seed -> identical output."""

import random
import re

from app.normalize import _COUNTRIES, fold, name_key

COUNTRY_WEIGHTS = {"DE": 22, "GB": 10, "NL": 8, "FR": 8, "IT": 6, "ES": 6, "AT": 5, "CH": 5,
                   "SE": 5, "PL": 5, "BE": 4, "US": 4, "FI": 3, "DK": 3, "NO": 3, "IN": 3}
TLD = {"DE": "de", "AT": "at", "CH": "ch", "NL": "nl", "BE": "be", "SE": "se", "FI": "fi",
       "DK": "dk", "NO": "no", "FR": "fr", "IT": "it", "ES": "es", "PL": "pl", "GB": "co.uk",
       "US": "com", "IN": "in"}
SUFFIXES = {"DE": ["GmbH", "AG", "GmbH & Co. KG"], "AT": ["GmbH"], "CH": ["AG", "GmbH"],
            "NL": ["B.V."], "BE": ["NV", "BV"], "SE": ["AB"], "FI": ["Oy"], "DK": ["ApS", "A/S"],
            "NO": ["AS"], "FR": ["SAS", "SA"], "IT": ["S.r.l.", "S.p.A."], "ES": ["S.L.", "S.A."],
            "PL": ["Sp. z o.o."], "GB": ["Ltd", "Limited"], "US": ["Inc.", "LLC"],
            "IN": ["Pvt Ltd"]}
SURNAMES = {
    "DE": ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Wagner", "Becker", "Köhler",
           "Hoffmann", "Krämer", "Schäfer", "Bäumer"],
    "AT": ["Gruber", "Huber", "Pichler", "Steiner", "Moser"], "CH": ["Keller", "Brunner",
           "Zürcher", "Frei"], "NL": ["de Vries", "Jansen", "Bakker", "Visser", "Smit"],
    "BE": ["Peeters", "Janssens", "Maes"], "SE": ["Lindqvist", "Sjöberg", "Åkesson", "Berg"],
    "FI": ["Virtanen", "Mäkinen"], "DK": ["Jørgensen", "Nielsen"], "NO": ["Haugen", "Solberg"],
    "FR": ["Dubois", "Lefèvre", "Moreau", "Girard"], "IT": ["Rossi", "Bianchi", "Ricci"],
    "ES": ["García", "Navarro", "Muñoz"], "PL": ["Kowalski", "Nowak", "Wiśniewski"],
    "GB": ["Thompson", "Clarke", "Hughes"], "US": ["Carter", "Brooks"], "IN": ["Sharma", "Iyer"],
}
BRANDS = ["Nordlicht", "Alpenwerk", "Bluefin", "Kestrel", "Helix", "Vantage", "Polaris",
          "Quantum", "Lumen", "Orbit", "Granite", "Silverline", "Brightwave", "Northstar",
          "Ironbridge", "Cobalt", "Meridian", "Tidewater", "Redwood", "Aurora", "Falkenstein",
          "Rheinblick", "Elbtal", "Seestern", "Zenit", "Pioneer", "Atlas", "Summit", "Beacon"]
CORE = {
    "automotive": ["Automotive", "Autoteile", "Fahrzeugtechnik", "Motion", "Drive Systems"],
    "fintech": ["Pay", "Finance", "Capital", "Payments", "Money"],
    "healthtech": ["Health", "Medical", "Care", "Diagnostics", "Clinic Systems"],
    "saas": ["Software", "Cloud", "Labs", "Analytics", "Digital"],
    "manufacturing": ["Maschinenbau", "Industries", "Werkzeuge", "Manufacturing", "Metallbau"],
    "ecommerce": ["Shop", "Commerce", "Retail", "Store", "Marketplace"],
    "logistics": ["Logistik", "Logistics", "Freight", "Transport", "Cargo"],
    "other": ["Consulting", "Media", "Partners", "Studio", "Events"],
}
INDUSTRY_WEIGHTS = {"automotive": 18, "fintech": 14, "healthtech": 12, "saas": 18,
                    "manufacturing": 12, "ecommerce": 9, "logistics": 9, "other": 8}
PARTS = ["brake systems", "seat frames", "wiring harnesses", "infotainment software",
         "sensor modules", "chassis parts", "battery cooling systems"]
CITIES = {"DE": ["Munich", "Stuttgart", "Hamburg"], "AT": ["Vienna", "Graz"],
          "CH": ["Zurich", "Basel"], "NL": ["Eindhoven", "Utrecht"], "BE": ["Antwerp", "Ghent"],
          "SE": ["Gothenburg", "Malmo"], "FI": ["Tampere", "Espoo"], "DK": ["Aarhus", "Odense"],
          "NO": ["Bergen", "Trondheim"], "FR": ["Lyon", "Toulouse"], "IT": ["Turin", "Bologna"],
          "ES": ["Valencia", "Bilbao"], "PL": ["Wroclaw", "Poznan"], "GB": ["Leeds", "Bristol"],
          "US": ["Austin", "Detroit"], "IN": ["Pune", "Chennai"]}

# Industry sentences say what the company does and nothing about compliance signals.
INDUSTRY_TEXT = {
    "automotive": [
        "We engineer {part} for passenger cars and commercial vehicles",
        "Our {part} end up in roughly one in five new cars sold in Europe",
        "{name} designs {part} for the automotive sector",
        "From prototype to series production, we deliver {part} for vehicle platforms",
        "Entwicklung und Fertigung von {part} für die Fahrzeugindustrie",
        "We make {part} that keep electric cars on the road",
        "Engineering partner for e-mobility: {part} and battery housings",
        "Our plants in {city} produce {part} for the mobility market",
    ],
    "fintech": [
        "{name} offers business accounts and corporate cards for SMEs",
        "We process card and wallet payments for online merchants",
        "Our app lets freelancers send invoices and get paid in one place",
        "Digital lending for small businesses, with decisions in 24 hours",
        "Wir bieten Zahlungslösungen für den Einzelhandel",
        "A neobank for expats living in Europe",
        "Treasury and FX tooling for mid-sized companies",
        "We help companies automate expense management and reimbursements",
    ],
    "healthtech": [
        "Software that helps hospitals schedule their operating rooms",
        "{name} runs a telemedicine service connecting patients with GPs",
        "We build practice management tools for dental clinics",
        "Remote monitoring for people with chronic heart conditions",
        "Digitale Lösungen für Krankenhäuser und Pflegeheime",
        "AI-assisted reading of radiology images",
        "An app that helps people manage their diabetes day to day",
        "Lab information systems for diagnostic laboratories in {city}",
    ],
    "saas": [
        "{name} builds B2B software for project planning",
        "A cloud platform for managing field service teams",
        "We make HR software for companies with 50 to 1,000 employees",
        "Analytics dashboards for marketing teams, delivered as SaaS",
        "Developer tools: an API for sending transactional email",
        "Our SaaS helps restaurants manage shifts and payroll",
        "Collaboration software for architecture firms",
        "Customer support software with a built-in knowledge base",
    ],
    "manufacturing": [
        "We manufacture industrial pumps and valves",
        "Family-owned producer of precision machine tools since 1962",
        "{name} makes packaging machinery for the food industry",
        "Maschinenbau: Sondermaschinen für die Kunststoffverarbeitung",
        "Contract manufacturer for sheet-metal parts",
        "We produce grippers and end-effectors for industrial robots",
        "Our factory in {city} produces HVAC components",
        "Producer of injection-moulded plastic parts for consumer goods",
    ],
    "ecommerce": [
        "{name} runs an online shop for outdoor gear",
        "A marketplace for refurbished electronics",
        "D2C brand selling organic skincare online",
        "We sell furniture through our web shop and two showrooms",
        "Online retailer of bicycle parts shipping across the EU",
        "Subscription box for specialty coffee, ordered online",
        "Our e-commerce store specialises in pet food",
        "Second-hand designer fashion, sold through our resale marketplace",
    ],
    "logistics": [
        "Freight forwarding between Europe and Asia",
        "{name} operates warehouses for e-commerce fulfilment",
        "Last-mile delivery with electric vans in {city}",
        "Spedition und Lagerlogistik für den Mittelstand",
        "Route planning and dispatch for truck fleets",
        "Cold-chain transport for temperature-sensitive goods",
        "We move containers by rail from the North Sea ports",
        "Customs brokerage and cross-border shipping for SMEs",
    ],
    "other": [
        "{name} is a marketing agency for B2B brands",
        "Management consulting for family businesses",
        "We produce corporate videos and podcasts",
        "Architecture and interior design studio based in {city}",
        "Recruiting agency for engineering talent",
        "Event management for trade fairs",
        "Translation and localisation services",
        "We operate co-working spaces in three cities",
    ],
}
SIGNAL_TEXT = {
    "automotive_supplier": [
        "We are a Tier-1 supplier to Volkswagen, BMW and Stellantis",
        "Most of our revenue comes from German OEMs",
        "Als Zulieferer beliefern wir namhafte Automobilhersteller",
        "Our customers include Mercedes-Benz and Audi",
        "We deliver straight to the assembly lines of major carmakers",
        "Tier 2 supplier for brake and steering systems",
        "OEM programmes account for 80% of our order book",
        "Our parts ship to Renault and Volvo Cars plants",
    ],
    "us_enterprise_customers": [
        "Half of our customers are US enterprises",
        "We recently signed our first Fortune 500 customer",
        "Expanding to the US in 2026 with an office in New York",
        "Our largest clients are American retail chains",
        "North American customers now make up 40% of revenue",
        "Enterprise buyers in the United States keep asking for our security documentation",
        "We sell to large companies in the US and Canada",
        "Customers in the US include several Fortune 100 banks",
    ],
    "hiring_security": [
        "We are hiring a Head of Information Security",
        "Open role: Security Engineer (m/w/d)",
        "Currently looking for a CISO to build our security team",
        "Join us as Compliance Manager, remote within the EU",
        "We're growing and hiring an IT security lead",
        "Our first dedicated security hire starts next quarter, apply now",
        "Stellenangebot: Informationssicherheitsbeauftragter (ISB)",
        "We are looking for a GRC analyst",
    ],
    "regulated_industry": [
        "Licensed by BaFin as a payment institution",
        "We are regulated by the Dutch central bank",
        "Our software processes patient data under strict health regulations",
        "As a medical device manufacturer we follow the MDR",
        "We have been subject to DORA since January 2025",
        "We fall under NIS2 as an essential entity",
        "Our clients are insurers and pension funds",
        "We hold an e-money licence",
    ],
}
FILLER = ["Founded in {year}", "Headquartered in {city}", "Around {n} people work with us",
          "We are a team of {n}", "Privately held since {year}", "Offices in {city} and Lisbon"]
FIRST = ["Jan", "Anna", "Lukas", "Sofia", "Emma", "Noah", "Lena", "Mateo", "Elif", "Jonas",
         "Mia", "Lars", "Chloé", "Giulia", "Marek", "Priya", "Aarav", "Olivia", "Hugo", "Freya"]
LAST = ["Weber", "Jansen", "Rossi", "Nowak", "Dubois", "Lind", "Okafor", "Kaya", "Silva",
        "Novak", "Iyer", "Brooks", "Fischer", "Bakker", "Moreau", "Haugen"]
TITLES = ["CISO", "CEO", "CTO", "Geschäftsführer", "Founder", "VP Engineering", "Head of IT",
          "Director of Operations", "IT Manager", "Compliance Manager", "Software Engineer",
          "Marketing Manager", "Sales Representative", "Office Manager", "Data Analyst"]
FREE_MAIL = ["gmail.com", "outlook.com", "gmx.de", "yahoo.com", "web.de", "hotmail.com"]
LEADS_PER_COMPANY = ([1] * 35 + [2] * 25 + [3] * 15 + [4] * 10 + [5] * 8 + [6] * 7)
_ASCII = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
                        "ß": "ss"})


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")


def _company(rng: random.Random, entity: int, taken: set) -> dict:
    countries, weights = zip(*COUNTRY_WEIGHTS.items(), strict=True)
    industries, iweights = zip(*INDUSTRY_WEIGHTS.items(), strict=True)
    country = rng.choices(countries, weights)[0]
    industry = rng.choices(industries, iweights)[0]
    for attempt in range(100):
        base = rng.choice(SURNAMES[country] if rng.random() < 0.6 else BRANDS)
        core = rng.choice(CORE[industry])
        if attempt > 50:
            core = f"{core} {attempt}"
        key = (country, name_key(f"{base} {core}"))
        if key not in taken:
            taken.add(key)
            break
    signals = []
    if industry == "automotive" and rng.random() < 0.7:
        signals.append("automotive_supplier")
    if rng.random() < 0.25:
        signals.append("us_enterprise_customers")
    if rng.random() < 0.25:
        signals.append("hiring_security")
    if rng.random() < {"fintech": 0.85, "healthtech": 0.85}.get(industry, 0.08):
        signals.append("regulated_industry")
    slug = _slug(f"{base} {core}")
    city = rng.choice(CITIES[country])
    fmt = {"name": f"{base} {core}", "part": rng.choice(PARTS), "city": city}
    signal_sentences = [rng.choice(SIGNAL_TEXT[s]) for s in signals]
    rng.shuffle(signal_sentences)
    sentences = [rng.choice(INDUSTRY_TEXT[industry]).format(**fmt), *signal_sentences]
    employees = rng.choice([8, 15, 25, 40, 60, 90, 120, 180, 250, 400, 650, 1200, 3000])
    sentences.append(rng.choice(FILLER).format(year=rng.randint(1950, 2022), city=city,
                                               n=employees))
    return {
        "entity": entity, "base": base, "core": core, "country": country,
        "suffix": rng.choice(SUFFIXES[country]), "domain": f"{slug}.{TLD[country]}",
        "alt_domain": f"{slug}-group.com", "employees": employees,
        "description": ". ".join(sentences) + ".",
        "labels": {"industry": industry, "signals": sorted(signals)},
    }


def _typo(rng: random.Random, word: str) -> str:
    letters = [i for i, c in enumerate(word) if c.isalpha()]
    if len(letters) < 4:
        return word
    i = rng.choice(letters[1:-1])
    if rng.random() < 0.5:
        return word[:i] + word[i + 1:]                       # drop a letter
    return word[:i] + word[i + 1] + word[i] + word[i + 2:]   # swap two letters


def _company_name(rng: random.Random, c: dict) -> str:
    base = c["base"]
    if rng.random() < 0.10:
        base = _typo(rng, base)
    if rng.random() < 0.30:
        base = base.translate(_ASCII)
    name = f"{base} {c['core']}"
    roll = rng.random()
    if roll < 0.45:
        name = f"{name} {c['suffix']}"
    elif roll < 0.55:
        name = f"{name} {rng.choice(SUFFIXES[c['country']])}"
    style = rng.random()
    if style < 0.10:
        name = name.upper()
    elif style < 0.20:
        name = name.lower()
    elif style < 0.25:
        name = f"  {name.replace(' ', '  ')} "
    return name


def _country_value(rng: random.Random, code: str) -> str:
    names = _COUNTRIES[code]
    return rng.choice([code, code.lower(), names[0].title(), rng.choice(names), f"{code} "])


def _employees_value(rng: random.Random, n: int) -> str | int:
    return rng.choice([n, str(n), f"{n:,}", f"{max(1, n - n // 5)}-{n + n // 5}"])


def _lead(rng: random.Random, c: dict) -> dict:
    first, last = rng.choice(FIRST), rng.choice(LAST)
    local = rng.choice([f"{first}.{last}", f"{first[0]}{last}", first]).lower()
    local = local.translate(_ASCII).replace("é", "e")
    if rng.random() < 0.20:
        email = f"{local}{rng.randint(1, 99)}@{rng.choice(FREE_MAIL)}"
    else:
        email = f"{local}@{c['alt_domain'] if rng.random() < 0.10 else c['domain']}"
    if rng.random() < 0.3:
        email = email.title() if rng.random() < 0.5 else f" {email} "
    lead = {"email": email, "name": f"{first} {last}", "title": rng.choice(TITLES),
            "company": _company_name(rng, c)}
    if rng.random() < 0.70:
        lead["website"] = rng.choice([f"https://www.{c['domain']}", c["domain"],
                                      f"http://{c['domain']}/about", f"www.{c['domain']}"])
    if rng.random() < 0.90:
        lead["country"] = _country_value(rng, c["country"])
    if rng.random() < 0.85:
        lead["employees"] = _employees_value(rng, c["employees"])
    if rng.random() < 0.70:
        lead["description"] = c["description"]
    return lead


def generate(seed: int, n_companies: int = 300) -> list[dict]:
    """Messy leads for n companies. Each lead dict is a webhook payload plus ground truth:
    _entity (company id), _labels (true industry/signals), _key (idempotency key)."""
    rng = random.Random(seed)
    taken: set = set()
    leads = []
    for entity in range(n_companies):
        company = _company(rng, entity, taken)
        for _ in range(rng.choice(LEADS_PER_COMPANY)):
            lead = _lead(rng, company)
            lead["_entity"], lead["_labels"] = entity, company["labels"]
            leads.append(lead)
    rng.shuffle(leads)
    for i, lead in enumerate(leads):
        lead["_key"] = f"s{seed}-{i}"
    return leads


if __name__ == "__main__":  # python -m bench.generate 1 5
    import json
    import sys

    for lead in generate(int(sys.argv[1]), int(sys.argv[2]))[:10]:
        print(json.dumps(lead, ensure_ascii=False))
