#!/usr/bin/env python3
"""
WOLF/ACCRUE/FA/2026.11  Wolf AI Labs  Financial Advisers pillar accrual

Stages
  1  FCA Register search    firm candidates by search term
  2  FCA Register details   status, permissions, address, website, individuals
  3  Website email match    named work emails on the firm's own website
  4  Output                 Brevo ready CSV (PILLAR = Financial Advisors)
  5  Brevo push (optional)  import into Brevo list WOLF_FA Register (id 45)

Environment (set on wolf-accrual-01, never in code)
  FCA_EMAIL        FCA Register API sign up email
  FCA_KEY          FCA Register API key
  BREVO_API_KEY    only needed with --push

Run
  python3 wolf_accrue_fa.py --out fa_register.csv
  python3 wolf_accrue_fa.py --out fa_register.csv --push
  python3 wolf_accrue_fa.py --selftest
"""
import argparse, csv, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from html.parser import HTMLParser

FCA_BASE = "https://register.fca.org.uk/services/V0.1"
BREVO_IMPORT = "https://api.brevo.com/v3/contacts/import"
BREVO_FA_LIST_ID = 45

SEARCH_TERMS = [
    "financial", "wealth", "advice", "advisers", "advisors", "planning",
    "IFA", "independent", "pensions", "mortgage", "investment", "asset",
    "private client", "retirement", "money", "chartered", "partners",
]

ADVISING_PERMISSION = re.compile(
    r"advising on (investments|pension transfers|regulated mortgage|home finance)"
    r"|arranging \(bringing about\) deals in investments"
    r"|advising on.*pension", re.I)

GENERIC_LOCAL = re.compile(
    r"^(info|admin|enquir|enquiries|hello|contact|office|mail|reception|support|"
    r"help|sales|accounts|team|compliance|careers|jobs|marketing|noreply|no-reply)", re.I)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CORP_SUFFIX = re.compile(r"\b(LTD|LIMITED|LLP|PLC|L\.L\.P\.|CIC)\b\.?$", re.I)
CONTACT_PATHS = ["", "/contact", "/contact-us", "/about", "/about-us", "/team",
                 "/our-team", "/meet-the-team", "/people", "/advisers", "/advisors"]


# ---------- helpers ----------
class Throttle:
    """FCA Register limit is 10 requests per 10 seconds; stay under it."""
    def __init__(self, per=9, window=10.0):
        self.per, self.window, self.stamps = per, window, []
    def wait(self):
        now = time.time()
        self.stamps = [t for t in self.stamps if now - t < self.window]
        if len(self.stamps) >= self.per:
            time.sleep(self.window - (now - self.stamps[0]) + 0.05)
        self.stamps.append(time.time())


def kget(d, *names, default=""):
    """Case and spacing insensitive key lookup, tolerant of API field naming."""
    if not isinstance(d, dict):
        return default
    norm = {re.sub(r"[^a-z0-9]", "", k.lower()): v for k, v in d.items()}
    for n in names:
        v = norm.get(re.sub(r"[^a-z0-9]", "", n.lower()))
        if v not in (None, ""):
            return v
    return default


def data_list(payload):
    d = payload.get("Data") if isinstance(payload, dict) else None
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return [d]
    return []


def postcode_area(pc):
    m = re.match(r"\s*([A-Za-z]{1,2})\d", pc or "")
    return m.group(1).upper() if m else ""


def split_name(full):
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    parts = [p for p in parts if p.lower().strip(".") not in ("mr", "mrs", "ms", "miss", "dr")]
    if not parts:
        return "", ""
    return parts[0].title(), parts[-1].title() if len(parts) > 1 else ""


def domain_of(url):
    if not url:
        return ""
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    host = urllib.parse.urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


# ---------- FCA client ----------
class FCA:
    def __init__(self, email, key):
        self.h = {"X-AUTH-EMAIL": email, "X-AUTH-KEY": key, "Accept": "application/json",
                  "Content-Type": "application/json",
                  "User-Agent": "Mozilla/5.0 (compatible; WolfRegister/1.0; +https://www.wolfailabs.com)"}
        self.t = Throttle()

    def get(self, path, params=None):
        url = FCA_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(3):
            self.t.wait()
            try:
                req = urllib.request.Request(url, headers=self.h)
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read().decode("utf-8", "replace")
                    if path == "/Search" and not getattr(self, "_logged200", False):
                        self._logged200 = True
                        print(f"  FCA 200 sample {url}: {raw[:400]}", flush=True)
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code in (500, 413) and path == "/Search":
                    return {"_overflow": True}
                if e.code == 404:
                    if path == "/Search" and not getattr(self, "_logged404", False):
                        self._logged404 = True
                        print(f"  FCA 404 {url}: {e.read()[:300]!r}", flush=True)
                    return {}
                if attempt == 2:
                    print(f"  FCA fail {path}: {e}", file=sys.stderr, flush=True)
                    return {}
                time.sleep(3 * (attempt + 1))
            except Exception as e:
                if attempt == 2:
                    print(f"  FCA fail {path}: {e}", file=sys.stderr, flush=True)
                    return {}
                time.sleep(3 * (attempt + 1))

    ALPHA = "abcdefghijklmnopqrstuvwxyz0123456789"

    def _search_once(self, term, max_pages=50):
        """Returns (frns, overflow)."""
        frns = set()
        for page in range(1, max_pages + 1):
            params = {"q": term, "type": "firm"}
            if page > 1:
                params["pgnp"] = page
            res = self.get("/Search", params)
            if isinstance(res, dict) and (res.get("_overflow") or str(res.get("Status")) == "413"):
                return frns, True
            rows = data_list(res)
            if not rows:
                break
            for r in rows:
                frn = str(kget(r, "Reference Number", "FRN", "ReferenceNumber")).strip()
                status = str(kget(r, "Status")).lower()
                if frn.isdigit() and ("authorised" in status or "registered" in status or not status):
                    frns.add(frn)
            ri = res.get("ResultInfo") if isinstance(res, dict) else None
            total = 0
            if isinstance(ri, dict):
                try:
                    tc, pp = int(ri.get("total_count") or 0), int(ri.get("per_page") or 0)
                    total = -(-tc // pp) if tc and pp else int(kget(ri, "total_page", "TotalPage", default=0) or 0)
                except (TypeError, ValueError):
                    total = 0
            if total and page >= total:
                break
            if not total and len(rows) < 20:
                break
        return frns, False

    def search_firms(self, term, depth=0, max_depth=3):
        frns, overflow = self._search_once(term)
        if not overflow:
            return frns
        if depth >= max_depth:
            print(f"  overflow kept at depth limit: {term!r}", flush=True)
            return frns
        sep = " " if depth == 0 else ""
        print(f"  splitting {term!r}", flush=True)
        for c in self.ALPHA:
            frns |= self.search_firms(term + sep + c, depth + 1, max_depth)
        return frns

    def firm(self, frn):
        out = {"frn": frn}
        d = data_list(self.get(f"/Firm/{frn}"))
        d = d[0] if d else {}
        out["name"] = kget(d, "Organisation Name", "Name")
        out["status"] = kget(d, "Status")
        perms = self.get(f"/Firm/{frn}/Permissions")
        pdata = perms.get("Data") if isinstance(perms, dict) else None
        names = list(pdata.keys()) if isinstance(pdata, dict) else [
            kget(p, "Permission", "Name") for p in data_list(perms)]
        out["advises"] = any(ADVISING_PERMISSION.search(str(n)) for n in names)
        a = data_list(self.get(f"/Firm/{frn}/Address"))
        a = a[0] if a else {}
        out["postcode"] = kget(a, "Postcode", "Post Code")
        out["website"] = kget(a, "Website Address", "Website", "URL")
        people = []
        for p in data_list(self.get(f"/Firm/{frn}/Individuals")):
            st = str(kget(p, "Status")).lower()
            if st and "current" not in st and "approved" not in st and "certified" not in st:
                continue
            people.append({"name": kget(p, "Name"), "irn": kget(p, "IRN")})
        out["people"] = people
        return out


# ---------- website email discovery ----------
class _Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.chunks = []
    def handle_data(self, d): self.chunks.append(d)
    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == "href" and v and v.lower().startswith("mailto:"):
                self.chunks.append(" " + v[7:].split("?")[0] + " ")


def site_emails(website, timeout=12):
    dom = domain_of(website)
    if not dom:
        return set()
    found = set()
    for path in CONTACT_PATHS:
        url = f"https://{dom}{path}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WolfRegister/1.0 (+https://www.wolfailabs.com)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                html = r.read(800_000).decode("utf-8", "replace")
        except Exception:
            continue
        p = _Text(); p.feed(html)
        for e in EMAIL_RE.findall(" ".join(p.chunks)):
            e = e.lower().strip(".")
            if e.endswith("@" + dom) or e.split("@")[1].endswith("." + dom):
                found.add(e)
        time.sleep(0.5)
    return found


def match_person(first, last, emails):
    """Return the named email that belongs to this person, never a generic box."""
    f, l = first.lower(), last.lower()
    if not f:
        return ""
    best = ""
    for e in emails:
        local = e.split("@")[0]
        if GENERIC_LOCAL.match(local):
            continue
        tokens = re.split(r"[._\-']", local)
        if l and (f in tokens and l in tokens):
            return e
        if l and (local in (f + l, f[0] + l, f[0] + "." + l, f + "." + l[0], l + "." + f, l + f[0])):
            return e
        if not l and local == f:
            best = e
        if l and local == f and not best:
            best = e
    return best


# ---------- pipeline ----------
FIELDS = ["EMAIL", "FIRSTNAME", "LASTNAME", "COMPANY", "PILLAR", "SOURCE", "BASIS",
          "SUBSCRIBER", "CATCHMENT", "POSTCODE", "WEBSITE", "FRN", "IRN"]


STATE_DIR = os.environ.get("STATE_DIR", "/data")


def _load(name, default):
    try:
        with open(os.path.join(STATE_DIR, name)) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(name, obj):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = os.path.join(STATE_DIR, name + ".tmp")
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, os.path.join(STATE_DIR, name))


def run(out_path, push, max_firms, batch=200):
    """Resumable sweep. State lives in STATE_DIR so a restart continues where it stopped."""
    em, ky = os.environ["FCA_EMAIL"].strip(), os.environ["FCA_KEY"].strip()
    print(f"  FCA creds: email={em[:3]}***@{em.split('@')[-1] if '@' in em else '?'} len={len(em)}, key len={len(ky)}", flush=True)
    fca = FCA(em, ky)
    probe = fca.get("/Firm/122702", {})
    print(f"  FCA probe /Firm/122702: {str(probe)[:300]}", flush=True)
    st = _load("fa_state.json", {"frns": [], "done": [], "emails": []})
    if not st["frns"]:
        frns = set()
        for t in SEARCH_TERMS:
            got = fca.search_firms(t)
            frns |= got
            print(f"search '{t}': {len(got)} firms, total {len(frns)}", flush=True)
        if not frns:
            raise RuntimeError("FCA search returned 0 firms; check FCA_EMAIL, FCA_KEY and API access")
        st["frns"] = sorted(frns)
        _save("fa_state.json", st)
    done, seen = set(st["done"]), set(st["emails"])
    todo = [f for f in st["frns"] if f not in done][: max_firms or None]
    out_exists = os.path.exists(out_path)
    pending = []
    with open(out_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if not out_exists:
            w.writeheader()
        for i, frn in enumerate(todo, 1):
            f = fca.firm(frn)
            if f["advises"] and f["people"]:
                emails = site_emails(f["website"])
                corp = bool(CORP_SUFFIX.search(str(f["name"]).strip()))
                for p in f["people"]:
                    first, last = split_name(p["name"])
                    email = match_person(first, last, emails)
                    if not email or email in seen:
                        continue
                    seen.add(email)
                    row = {
                        "EMAIL": email, "FIRSTNAME": first, "LASTNAME": last, "COMPANY": f["name"],
                        "PILLAR": "Financial Advisors", "SOURCE": "REGISTER",
                        "BASIS": "LEGIT_INTEREST" if corp else "CONSENT_REQUIRED",
                        "SUBSCRIBER": "CORPORATE" if corp else "INDIVIDUAL",
                        "CATCHMENT": postcode_area(f["postcode"]), "POSTCODE": f["postcode"],
                        "WEBSITE": f["website"], "FRN": frn, "IRN": p["irn"]}
                    w.writerow(row); pending.append(row)
            done.add(frn)
            if i % 25 == 0 or i == len(todo):
                fh.flush()
                if push and len(pending) >= batch or (push and i == len(todo) and pending):
                    brevo_push(pending); pending = []
                st["done"], st["emails"] = sorted(done), sorted(seen)
                _save("fa_state.json", st)
                print(f"{len(done)}/{len(st['frns'])} firms, named emails {len(seen)}", flush=True)
    print(f"SWEEP DONE firms {len(done)}, named emails {len(seen)} -> {out_path}", flush=True)


def worker(push):
    """Railway mode: full sweep, then restart the sweep every 24 hours."""
    out = os.path.join(STATE_DIR, "fa_register.csv")
    while True:
        try:
            run(out, push, 0)
            st = _load("fa_state.json", {})
            st["frns"], st["done"] = [], []
            _save("fa_state.json", st)
            time.sleep(24 * 3600)
        except Exception as e:
            print("worker error:", e, flush=True)
            time.sleep(3600)


def brevo_push(rows):
    key = os.environ["BREVO_API_KEY"]
    cols = ["EMAIL", "FIRSTNAME", "LASTNAME", "COMPANY", "PILLAR", "SOURCE",
            "BASIS", "SUBSCRIBER", "CATCHMENT", "POSTCODE", "WEBSITE"]
    for i in range(0, len(rows), 5000):
        chunk = rows[i:i + 5000]
        body = ";".join(cols) + "\n" + "\n".join(
            ";".join(str(r[c]).replace(";", ",") for c in cols) for r in chunk)
        payload = json.dumps({"fileBody": body, "listIds": [BREVO_FA_LIST_ID],
                              "updateExistingContacts": True,
                              "emptyContactsAttributes": False}).encode()
        req = urllib.request.Request(BREVO_IMPORT, data=payload, method="POST", headers={
            "api-key": key, "Content-Type": "application/json", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            print("Brevo import", r.status, r.read().decode()[:200], flush=True)


# ---------- offline self test ----------
def selftest():
    assert postcode_area("M2 4WQ") == "M" and postcode_area("ML4 3FB") == "ML"
    assert split_name("Mr John Andrew Smith") == ("John", "Smith")
    assert domain_of("www.zestfinancial.co.uk") == "zestfinancial.co.uk"
    em = {"info@acme.co.uk", "john.smith@acme.co.uk", "jsmith@acme.co.uk", "sarah@acme.co.uk"}
    assert match_person("John", "Smith", em) in ("john.smith@acme.co.uk", "jsmith@acme.co.uk")
    assert match_person("Mary", "Jones", em) == ""
    assert match_person("Sarah", "Brown", em) == "sarah@acme.co.uk"
    assert ADVISING_PERMISSION.search("Advising on investments (except on Pension Transfers and Pension Opt Outs)")
    assert CORP_SUFFIX.search("ZEST FINANCIAL LIMITED") and not CORP_SUFFIX.search("J Smith Financial Planning")
    assert kget({"Organisation Name": "X"}, "organisationname") == "X"
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="fa_register.csv")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--max-firms", type=int, default=0)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--worker", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.worker:
        worker(a.push)
    else:
        run(a.out, a.push, a.max_firms)
