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

# ---------------------------------------------------------------------------
# Matching logic ported from Qualified-Leads-Tagging/2-analysis/response-time/
# scripts/infer_gender.py. Key principles that fix the substring-matcher bugs:
#   1. EXACT dictionary match wins first (so "Irfan" -> male before any prefix
#      rule can rewrite it to "irfa").
#   2. Gender the FIRST informative token only — Pakistani names are
#      firstname-fathername-surname, so token 1 is the reliable signal.
#   3. Muhammad/Syed etc. are prefixes: skip to the next informative token.
#   4. Fuzzy matching (prefix / embedded-strong-name / suffix) is a graded
#      fallback used ONLY when no exact match exists.
# The JSON dictionaries are unioned with curated first-name lists carried over
# from the reference, adding common names our JSON was missing (irfan, junaid,
# muneeb, wazeer, ...).
# ---------------------------------------------------------------------------

# Curated first-name lists (from infer_gender.py) — merged with the JSON dicts.
_CURATED_M = {
    'ali','hassan','hasan','husain','hussain','hussein','ahmed','ahmad','akhtar','akram',
    'bilal','usman','umar','umer','hamza','talha','anas','anus','zaid','zayd','zubair',
    'hamid','hamzah','ibrahim','yusuf','yousuf','yusaf','yousaf','yasir',
    'khan','sheikh','mughal','raza','rizwan','rashid','rehan','rauf','razzaq',
    'abdullah','abdul','abdurrahman','abdurrehman','adnan','adeel','adil','ahsan',
    'saad','sajjad','salman','saqib','sami','sameer','samir','sajid','sarmad',
    'faisal','farhan','furqan','fahad','farooq','fawad','faraz',
    'junaid','asad','awais','ayan','arsal','arsalan','arham','ammar','amir',
    'imran','jawad','kashif','khurram','kamran','kabir','kaleem','khalid',
    'naveed','nadeem','naeem','nauman','naseer','noman','nasir','nazim',
    'owais','qasim','qadir','qamar','rabbani',
    'shahid','shahbaz','shoaib','shahzad','sheraz','sufyan','sohaib','suleman','sulaiman',
    'tariq','tahir','talal','tayyab','taimoor','tanveer','tahseen','tanzeel',
    'wasim','waqar','waleed','wajid','walid',
    'zaheer','zafar','zahid','zia','zameer','zohaib',
    'atif','anwar','arif','asim','aaqib','aqib',
    'bilawal','basit','basir','burhan','bashir',
    'danish','daniyal','daud',
    'eshaq','ehsan','emad','ehtesham',
    'gulraiz','ghaffar','ghazi',
    'haider','hafeez','haris','hashim','hashir','haseeb',
    'iftikhar','iqbal','irfan','ihsan','ismail','ismaeel',
    'javed','jamal','jameel','jibran','juzar',
    'kazim','kifayat',
    'liaqat','luqman',
    'mahad','mahmood','mansoor','mashood','maqsood','mateen','mehmood','mubashir','muddasir',
    'muhammad','mohammad','mohammed','mujtaba','munir','muneer','musa','musab','mustansar','mustafa',
    'nabeel','nadir','najam','naqi','nizam','nasrullah',
    'rafay','rafi','rafiq','rais','rajab','rehmat',
    'sadiq','sahil','salah','salahuddin','salim','samiullah','sarfaraz','seemab',
    'tahmid','taj','tajamul','tehseen','toseef','tufail',
    'ubaid','ubaidullah','umair','usaid','usaim','usama','uzair',
    'wahab','wajahat','waris','wazir',
    'yahya','yameen','yasin',
    'zaeem','zain','zuhair','zulqarnain',
    'shafiullah','shadab','shabbir','siraj','sufian','sufiyan','sultan','sumair','sunny','syed',
    'muqeem','muneeb','sahir','sajawal','sammar','shams',
    'rehman','abbas','faizan','touqir','dawood','nouman','saleem','asif','ayaan','murtaza',
    'mehdi','taha','zeeshan','rayyan','sabir','hafiz','altaf','mir','meer','rafique',
    'rahim','ramzan','raffay','abdulraffay','abdulhaseeb',
    'kashan','arsh','aarav','aryan','aariz','aizaz','akbar','alam','aman',
    'anwer','aqeel','arman','arshad','azan','azhar','azim','baqir',
    'daniyal','dilshad','ejaz','farid','fasih','feroz','ghafoor','gulfam',
    'hammad','haroon','hatim','idrees','imdad','irshad','ishaq',
    'jalal','jamil','jibreel','kamil','kareem','karim','kazi','khaliq',
    'mahmoud','majid','mamun','masood','mazhar','mehboob','minhaj',
    'mubeen','mudassir','muizz','mujahid','murad','musharraf',
    'naasir','nafees','najeeb','naseem','naufal','nazir','niaz',
    'parvez','qayyum','qutub','raheel','rahman','rasheed',
    'razi','riaz','sabeel','sadaqat','safdar','sahab','sayed','shafiq','shahab','shariq',
    'shoukat','sikandar','suhail','tabish','tasawar',
    'waqas','waseem','yaqoob','yousaf','zahoor',
    'zaman','zarar','zayn',
    'ahtsham','jappa','raja',
    'huzail','hadi','masab','subhan','alishah','shafaqat','moeen','ijaz','mukesh',
    'mahesh','ramesh','basharat','emmanuel','mahathir','mahateer','adeeb','sadam','sadaam',
    'shakeel','aftab','rajeev','rakesh','suresh','rohit','sumit','vivek','vinay','vipul',
    'amjad','akhter','ashfaq','azeem','bakht','dilawar','elyas','fahim','fakhar','gulzar',
    'gulshan','haq','imdadullah','inamullah','intazar','irfanullah','israr','jameelullah',
    'kabeer','khaleel','liaquat','majeed','maqbool','mehran','mubasher','musaib','nazeer',
    'noorullah','pervaiz','rahmat','rashed','sajidullah','saleemullah',
    'sarwar','shabir','shahabuddin','shahnawaz','shahzada','sharif',
    'shaukat','shujaat','sibghatullah','siddique','tajammul','vakeel',
    'wahid','younis','zafarullah','zahidullah','zakariya','zulfiqar',
    'inayat',  # common male name our JSON lacked (Inayat Ali / Inayatullah)
}
_CURATED_F = {
    'aisha','ayesha','aiesha','asma','asmaa','anam','anum','anushka','aliza',
    'amna','amina','aamna','amber','ammara','anila','aroob','areeba','areej',
    'aizal','aiman','aimal','aniqa','arzoo','ayat','ayeza','asfa','asia',
    'baseera','batool','bushra','bisma','bia','bilqis',
    'eman','eshal','eshaal','elma',
    'fatima','fareeha','fariha','farah','farzana','fauzia','fizza','fakhira',
    'gulnaz','gul','gulsehar','ghazala',
    'hanifa','hooria','hoor','huma','hira','hina','humera','hafsa','hadiah','hafeeza','hareem',
    'iqra','isha','iram','irum','irsa','izza','izna',
    'jamila','javeria','juveria',
    'khadija','kanwal','kainat','khansa','kiran','kashaf','komal',
    'laiba','lubaina','laraib',
    'maryam','mariam','mehnaz','mehwish','maham','mahnoor','marwa','manahil','mahima','mehreen',
    'mishal','minahil','mubashra','muqaddas',
    'nadia','naila','najma','nida','nazia','nimra','natasha',
    'nashita','neelam','nooria','nosheen','nuzhat',
    'palwasha','parisa',
    'qudsia','qurat',
    'rabia','rabbiyah','rida','rimsha','ruqayya','ramsha','romana','ruqaiya','rumaisa',
    'saba','sabeen','sadia','saima','samia','sana','sania','sarah','sehrish','simran',
    'shazia','shamim','sundus','surriya','sundas','syeda','sara','sehar',
    'shabnam','shahnaz','shaila','shaina','shumaila','sidra','sila','siyana','sumera','sumaira',
    'tahira','tania','tooba','tahmina','tamana','taqdees','tehreem','tina',
    'uzma','umama','uroosa','usaira',
    'wajiha','warda',
    'yasmin','yusra','yasmeen','yashfa','yumna',
    'zahra','zaib','zainab','zara','zoya','zoha','ziva','zubaida','zunaira',
    'maliha','maira','mariyam','musfira','muskan','mahum',
    'shafia','shazma','shifa',
    'anoshay','myra','khanzaadi','sahar','aleeza','arooj','ayza','beenish','dua',
    'erum','farwa','hooriya','kainaat','laila','mahek',
    'mahveen','marium','mehak','minha','mishaal','nayab','nimrah','nyla','rabail',
    'roohi','rumana','sabahat','tabinda',
    'tehmina','wania','yashfeen','zaynab','zunairah','aleena','arwa',
    'mareeha','nabila','nargis','nayla','saira','salma','urwa','wajeeha','fiza','anamta','anamtaa',
    'isma','rabab','aneeta','aneetaa',
    'safia','ruby','sonia','ifra','kashmala','hania','haniagul','bareera','hadaiq','sumaiya',
    'prerna','emaan','murk','ezzah','meloo','masha','radhika','rachna','priya','pooja',
    'reena','suman','sapna','geeta','seema','neha','kavita','radha','shanaya',
    'rukhsana','rukhsar','tasneem','tehmeena','tehzeeb','umaima','umme',
    'unaiza','unsa','wardah','yashma','zaiba',
    'zarmeena','zehra','zikra','zohra','zulekha','samira','sameen',
    'shumail','sumayya','sumayyah','sunaina','swera','sweera',
    'tanzila','taqseema','tasleem','tehniat','urooj','urooba','warisha',
    'rifa','rohani','ruheen','ruqia','samrah','samreen',
    'sehrosh','shabana','shaheen','shaista','shamaila','shanze','shazra','sheherbano',
    'shireen','sidrah','sitara','sufia','suniya','tabassum','tahmeena','taiba',
    'abiha','urfa','shandana','arfa','uzmaa',  # common female names our JSON lacked
}

FEMALE = set(_gd["female"]) | set(_manual["female"]) | _CURATED_F
MALE = set(_gd["male"]) | set(_manual["male"]) | _CURATED_M

# Canonical corrections for names mis-listed / ambiguous in the base JSON
# dictionaries. Several common names appear in BOTH JSON lists (dirty data),
# which would otherwise resolve to Uncategorized; pin them to the correct gender.
_FORCE_MALE = {"najaf", "huzaifa"}                       # Najaf (Ali), Huzaifa — clear male
_FORCE_FEMALE = {"fatiha", "nadia", "naila", "nayab",    # clear female names stuck in the
                 "aiman", "arwa", "kashaf"}              # male/female overlap or missing
MALE = (MALE | _FORCE_MALE) - _FORCE_FEMALE
FEMALE = (FEMALE | _FORCE_FEMALE) - _FORCE_MALE

# Prefix words to skip (generic honorifics / compound-name lead-ins) + titles.
PREFIX_WORDS = {"muhammad", "mohammad", "mohammed", "mohamad", "syed", "sayyad",
                "sayyed", "syeda", "mr", "mrs", "miss", "ms", "dr", "sir", "madam"}
FEMALE_TITLES = {"mrs", "miss", "ms", "madam"}
MALE_TITLES = {"mr", "sir"}

# Small, curated lists of strong names that commonly appear glued inside a token
# (e.g. "zainabrehman"). Deliberately short — NOT the whole dictionary — so we
# don't reintroduce the substring false-matches.
STRONG_F = ("zainab", "fatima", "ayesha", "aisha", "khadija", "maryam", "amna",
            "hira", "laiba", "iqra", "aliza", "mariam", "hafsa", "kashmala",
            "sumaiya", "anoshay", "wajeeha", "sehrish", "rimsha")
STRONG_M = ("muhammad", "mohammad", "hassan", "hussain", "hasnain", "fahad",
            "bilal", "farooq", "rehman", "shahzad", "arsalan", "imran", "kashif",
            "adeel", "hamza", "rafay", "asad", "ahmed", "ahmad", "mujtaba",
            "sufyan", "sufian", "modassir", "adnan", "imtiaz", "obaid", "abrar",
            "waryam", "iqbal", "azfar", "zeeshan")
MALE_SUFFIXES = ("ullah", "uddin", "uddeen")
FEMALE_SUFFIXES = ("bibi", "begum", "khatoon")


def _latin_clean(name: str) -> str:
    """Lowercase; keep letters, spaces, apostrophes, hyphens; collapse whitespace."""
    if not name:
        return ""
    name = name.lower()
    name = re.sub(r"[^a-z\s'\-]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _match_chunk(c: str):
    """Match one token against the name sets. Returns 'Female'/'Male'/None.
    Priority: exact > strong-embedded-female > prefix > strong-embedded-male > suffix."""
    if not c or len(c) < 3 or c in PREFIX_WORDS:
        return None
    # 1) exact match wins (fixes Irfan/Junaid/... being rewritten by prefix rules)
    if c in FEMALE and c not in MALE: return "Female"
    if c in MALE and c not in FEMALE: return "Male"
    if c in FEMALE and c in MALE: return None  # genuinely ambiguous token
    # 2) strong female name embedded in a glued token
    for nm in STRONG_F:
        if nm in c and len(c) > len(nm): return "Female"
    # 3) prefix match (longest wins; ties -> female), so "aneetaimran" -> female
    best = None
    for nm in FEMALE:
        if len(nm) >= 4 and c.startswith(nm) and (best is None or len(nm) > best[0]):
            best = (len(nm), "Female")
    for nm in MALE:
        if len(nm) >= 4 and c.startswith(nm) and (best is None or len(nm) > best[0]):
            best = (len(nm), "Male")
    if best:
        return best[1]
    # 4) strong male name embedded in a glued token
    for nm in STRONG_M:
        if len(nm) >= 5 and nm in c and len(c) > len(nm): return "Male"
    # 5) suffix heuristics
    if len(c) >= 6 and any(c.endswith(s) for s in MALE_SUFFIXES): return "Male"
    if len(c) >= 5 and any(c.endswith(s) for s in FEMALE_SUFFIXES): return "Female"
    return None


def _infer_from_email(email: str):
    """Gender signal from the email local-part; prefer the first chunk (likely first name)."""
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0].lower()
    chunks = [c for c in re.split(r"[^a-z]+", local) if len(c) >= 3]
    if not chunks:
        return None
    g = _match_chunk(chunks[0])
    if g:
        return g
    for chunk in chunks[1:]:
        g = _match_chunk(chunk)
        if g:
            return g
    return None


def _infer_from_name(name: str):
    """Gender from a (normalized) name string. Returns 'Female'/'Male'/None."""
    tokens = name.split()
    if not tokens:
        return None
    # Title prefix
    if tokens[0] in FEMALE_TITLES: return "Female"
    if tokens[0] in MALE_TITLES: return "Male"
    # Arabic compound "X ul/ud/un Y" (Noor ul Amin, Zia ul Haq) — always male
    if re.search(r"\b(ul|ud|un)\s+\w+", " ".join(tokens)):
        return "Male"
    # Skip generic prefix words; gender the first informative token
    informative = [t for t in tokens if t not in PREFIX_WORDS and len(t) > 1]
    candidates = informative if informative else tokens
    g = _match_chunk(candidates[0])
    if g:
        return g
    for tok in candidates[1:]:
        g = _match_chunk(tok)
        if g:
            return g
    # Bare "muhammad"/"syed" only → male
    if all(t in PREFIX_WORDS for t in tokens):
        return "Male"
    return None


def classify(name: str, email: str = "") -> str:
    """Main entry point. Returns 'Female', 'Male', or 'Uncategorized'.

    Order: name (exact → fuzzy) → email rescue. Input is first Unicode-normalized
    (NFKD) and Arabic-script tokens are transliterated, then Latin-cleaned."""
    name = _latin_clean(normalize_input(name))
    g = _infer_from_name(name) if name else None
    if g:
        return g
    g = _infer_from_email(normalize_input(email))
    if g:
        return g
    return "Uncategorized"


if __name__ == "__main__":
    # quick smoke test — includes names that previously misclassified
    cases = [("Ayesha Khan", ""), ("Muhammad Ali", ""), ("Saqib", ""), ("Mrs. Rashid", ""),
             ("Abdullah", ""), ("🦋", ""), ("", "fatima@x.com"),
             ("Irfan Ullah", ""), ("Junaid", ""), ("Abiha Asim", ""), ("Urfa Zaheer", ""),
             ("Shandana", ""), ("Muneeb Ansari", ""), ("Noor ul Amin", "")]
    for n, e in cases:
        print(f"  classify({n!r:<25}, {e!r:<20}) -> {classify(n, e)}")
