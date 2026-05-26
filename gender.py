"""Gender classifier for South-Asian names (Pakistani-leaning).

Usage:
    from gender import classify
    classify("Ayesha Khan", "ayesha@x.com")  # -> "Female"
    classify("Muhammad Ali", "")             # -> "Male"
    classify("🦋", "x@y.com")                 # -> "Uncategorized"

Returns one of: "Female", "Male", "Uncategorized".

To port to another project, copy this file plus the two JSON dictionaries:
    dictionaries/name_gender_lookup.json   (base dictionary)
    dictionaries/manual_classified.json    (manual overrides / additions)

Both JSONs follow the shape: {"female": [...], "male": [...]}.
The path to the dictionaries directory is resolved relative to this file —
override by setting the GENDER_DICT_DIR env var.
"""
import os, re, json, unicodedata
from pathlib import Path

# Arabic-script → Latin mapping for common Pakistani/Muslim names.
# Applied BEFORE token extraction so the existing English dictionary can match.
ARABIC_NAME_MAP = {
    # Male
    "محمد":"muhammad","محمّد":"muhammad","مُحمد":"muhammad",
    "علی":"ali","علي":"ali","حسن":"hassan","حسین":"hussain","حُسین":"hussain",
    "احمد":"ahmed","أحمد":"ahmed","عمر":"umar","عثمان":"usman","عُثمان":"usman",
    "بلال":"bilal","حمزہ":"hamza","حمزة":"hamza","خالد":"khalid",
    "یوسف":"yousuf","یُوسف":"yousuf","ابراہیم":"ibrahim","ابراهيم":"ibrahim",
    "اسماعیل":"ismail","موسیٰ":"musa","طلحہ":"talha","سعد":"saad",
    "عبداللہ":"abdullah","عبدالله":"abdullah","عبدالرحمٰن":"abdulrahman","عبدالرحمن":"abdulrahman",
    "زبیر":"zubair","جنید":"junaid","فہد":"fahad","وقاص":"waqas",
    "شعیب":"shoaib","شاہد":"shahid","اسد":"asad","زید":"zaid","ولید":"waleed",
    "ندیم":"nadeem","وسیم":"waseem","طارق":"tariq","ارشد":"arshad",
    "اسحاق":"ishaq","یعقوب":"yaqub","داؤد":"dawood","صلاح":"salah",
    "بشیر":"bashir","رفیق":"rafiq","شفیق":"shafiq","نعمان":"numan","کاشف":"kashif",
    "حماد":"hammad","ابوبکر":"abubakar","ابراز":"abraz","عرفان":"irfan",
    "سلیمان":"sulaiman","ابو":"abu","سید":"syed","شاہ":"shah","خان":"khan","میر":"mir","پیر":"peer",
    # Female
    "فاطمہ":"fatima","فاطمة":"fatima","عائشہ":"ayesha","عائشة":"ayesha",
    "خدیجہ":"khadija","خديجة":"khadija","زینب":"zainab","زینب":"zainab",
    "مریم":"maryam","آمنہ":"amna","آمنة":"amna","حفصہ":"hafsa","حفصة":"hafsa",
    "صفیہ":"safiya","ہاجرہ":"hajra","سارہ":"sara","ہانیہ":"hania","ایمن":"ayman",
    "صائمہ":"saima","صوفیہ":"sofia","ثنا":"sana","سنا":"sana","شاہین":"shaheen",
    "اسماء":"asma","صبا":"saba","حنا":"hina","حناء":"hina","نیلم":"neelam","زویا":"zoya",
    "نازیہ":"nazia","ہما":"huma","ربیعہ":"rabia","آصفہ":"asifa","نصرت":"nusrat",
    "بشریٰ":"bushra","جویریہ":"juwairia","نفیسہ":"nafisa","سحر":"sehar","سحرش":"seharish",
    "ام":"umm","بیگم":"begum","بی":"bibi","سیدہ":"syeda","رابعہ":"rabia","رومیسا":"rumaisa",
    "علیزہ":"aleeza","لیلیٰ":"laila","ہالہ":"hala","نور":"noor","ایمان":"iman",
    "ارفعہ":"arfa","عظمیٰ":"uzma","نسرین":"nasreen","شمسہ":"shamsa","رخسانہ":"rukhsana",
    "اقرا":"iqra","انعم":"anam","حُریرہ":"hurraira",
    # Common surname/honorific
    "بنت":"bint",
}
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿ]+")
def _arabic_transliterate(s: str) -> str:
    """Replace Arabic-script tokens with their Latin equivalents. Unmapped tokens are dropped."""
    if not _ARABIC_RE.search(s): return s
    def repl(m):
        w = m.group(0)
        # exact map first
        if w in ARABIC_NAME_MAP: return " " + ARABIC_NAME_MAP[w] + " "
        # try stripping common diacritics
        bare = re.sub(r"[ً-ٰٟ]", "", w)
        if bare in ARABIC_NAME_MAP: return " " + ARABIC_NAME_MAP[bare] + " "
        return " "
    return _ARABIC_RE.sub(repl, s)

def normalize_input(s: str) -> str:
    """Apply Arabic + Unicode normalization before classification."""
    if not s: return s
    # 1) Arabic transliteration FIRST (so NFKD doesn't decompose Arabic codepoints)
    s = _arabic_transliterate(s)
    # 2) NFKD strips stylized math/font variants (𝓜𝓾𝓱𝓪𝓶𝓶𝓪𝓭 -> Muhammad)
    s = unicodedata.normalize("NFKD", s)
    # Drop combining marks left after NFKD
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s

_DICT_DIR = Path(os.environ.get("GENDER_DICT_DIR") or (Path(__file__).parent / "dictionaries"))

_gd = json.load(open(_DICT_DIR / "name_gender_lookup.json"))
_manual = json.load(open(_DICT_DIR / "manual_classified.json"))

FEMALE = set(_gd["female"]) | set(_manual["female"])
MALE = set(_gd["male"]) | set(_manual["male"])
ALL_TOKENS = sorted(FEMALE | MALE, key=lambda x: -len(x))  # longest-first for prefix/substring matching

FEMALE_MARKERS = {"bibi", "begum", "khatun", "khanum", "mrs", "miss", "ms"}
MALE_SUFFIXES = ("ullah", "uddin", "ud-din", "uddeen")
EXPLICIT_F = {"girl", "queen", "princess", "mother", "sister", "lady", "madam", "wife", "daughter", "auntie"}
EXPLICIT_M = {"boy", "king", "prince", "father", "brother", "sir", "husband", "son", "uncle"}


def extract_tokens(text: str) -> str:
    """Split text into tokens; rewrite each token to a known dictionary token via prefix/substring."""
    if not text: return ""
    if "@" in text: text = text.split("@", 1)[0]
    text = text.lower()
    parts = re.split(r"[^a-z]+", text)
    out = []
    for p in parts:
        if not p or len(p) < 2: continue
        matched = False
        for tok in ALL_TOKENS:
            if len(tok) >= 4 and p.startswith(tok):
                out.append(tok); matched = True; break
        if not matched:
            for tok in ALL_TOKENS:
                if len(tok) >= 4 and tok in p:
                    out.append(tok); matched = True; break
        if not matched:
            out.append(p)
    return " ".join(out)


def _classify(text: str) -> str:
    """Dictionary lookup. Returns 'Female', 'Male', or 'Uncategorized'."""
    if not text: return "Uncategorized"
    n = re.sub(r"[^a-zA-Z\s]", " ", text.lower()).strip()
    if not n: return "Uncategorized"
    tokens = n.split()
    male_hit = female_hit = False
    for t in tokens:
        if t in FEMALE: female_hit = True
        if t in MALE: male_hit = True
    if female_hit and not male_hit: return "Female"
    if male_hit and not female_hit: return "Male"
    if female_hit and male_hit:
        first = tokens[0]
        if first in FEMALE: return "Female"
        if first in MALE: return "Male"
    return "Uncategorized"


def _structural(text: str) -> str:
    """Structural fallback: explicit markers (bibi/begum), male suffixes (-ullah/-uddin), explicit nouns."""
    if not text: return "Uncategorized"
    tokens = re.split(r"[^a-z]+", text.lower())
    for tok in tokens:
        if not tok: continue
        if tok in FEMALE_MARKERS: return "Female"
        if tok in EXPLICIT_F: return "Female"
        if tok in EXPLICIT_M: return "Male"
        if len(tok) >= 6 and any(tok.endswith(suf) for suf in MALE_SUFFIXES): return "Male"
    return "Uncategorized"


def classify(name: str, email: str = "") -> str:
    """Main entry point. Tries name (dict) -> email (dict) -> name (structural) -> email (structural).
    Input is first Unicode-normalized (NFKD) and Arabic-script tokens are transliterated."""
    name = normalize_input(name)
    email = normalize_input(email)
    g = _classify(extract_tokens(name))
    if g != "Uncategorized": return g
    g = _classify(extract_tokens(email))
    if g != "Uncategorized": return g
    g = _structural(name)
    if g != "Uncategorized": return g
    return _structural(email) or "Uncategorized"


if __name__ == "__main__":
    # quick smoke test
    cases = [("Ayesha Khan", ""), ("Muhammad Ali", ""), ("Saqib", ""), ("Mrs. Rashid", ""),
             ("Abdullah", ""), ("🦋", ""), ("", "fatima@x.com")]
    for n, e in cases:
        print(f"  classify({n!r:<25}, {e!r:<20}) -> {classify(n, e)}")
