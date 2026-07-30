#!/usr/bin/env python3
"""researchmap 同期スクリプト

researchmap の公開 API から全項目・基本情報・顔写真を取得し、
Hugo 用のデータファイルとお知らせ記事(Markdown)を生成する。

- 取得対象は MEMBERS のリスト(複数の ID を指定すると全員分を統合して表示できる)
- 初回実行時は既存項目を「既知」として記録するだけで、お知らせ記事は作らない
  (2回目以降、新しく増えた項目だけが記事になる)
- API に到達できない場合は何も書き換えずに終了する(サイトは壊れない)

使い方:  python3 scripts/fetch_researchmap.py
"""

import html as htmllib
import json
import re
import sys
import unicodedata
import urllib.request
from datetime import date
from pathlib import Path

# ======================== 設定 ========================

MEMBERS = ["okada_n"]  # 複数人の業績を統合したい場合はここに ID を追加
PRIMARY = "okada_n"    # 基本情報(氏名・所属・顔写真)に使う ID

# 自動お知らせ記事を作る項目と文面テンプレート
NEWS_TEMPLATES = {
    "published_papers": {
        "ja": "(論文)「{title}」が『{venue}』に掲載されました。",
        "en": '(Paper) "{title}" has been published in {venue}.',
        "ja_novenue": "(論文)「{title}」が公開されました。",
        "en_novenue": '(Paper) "{title}" has been published.',
    },
    "misc": {
        "ja": "(MISC)「{title}」が公開されました。",
        "en": '(MISC) "{title}" has been published.',
    },
    "presentations": {
        "ja": "(講演・口頭発表)「{title}」を{venue}で発表しました。",
        "en": '(Presentation) Presented "{title}" at {venue}.',
        "ja_novenue": "(講演・口頭発表)「{title}」を発表しました。",
        "en_novenue": '(Presentation) Presented "{title}".',
    },
    "awards": {
        "ja": "(受賞){title}を受賞しました。",
        "en": "(Award) Received {title}.",
    },
    "research_projects": {
        "ja": "(研究課題)「{title}」が採択されました。",
        "en": '(Project) Research project "{title}" has been funded.',
    },
    "committee_memberships": {
        "ja": "(委員歴){venue} {title}に就任しました。",
        "en": "(Committee) Appointed {title}, {venue}.",
        "ja_novenue": "(委員歴){title}に就任しました。",
        "en_novenue": "(Committee) Appointed {title}.",
    },
    "association_memberships": {
        "ja": "(所属学協会){title}に入会しました。",
        "en": "(Membership) Joined {title}.",
    },
    "teaching_experience": {
        "ja": "(担当科目){title}を担当します。",
        "en": "(Teaching) Teaching {title}.",
    },
    "social_contribution": {
        "ja": "(社会貢献){title}を実施しました。",
        "en": "(Outreach) {title}.",
    },
    "media_coverage": {
        "ja": "(メディア報道){venue}に掲載されました:「{title}」",
        "en": "(Media) Featured in {venue}: {title}.",
        "ja_novenue": "(メディア報道)「{title}」で研究が紹介されました。",
        "en_novenue": "(Media) Our research was featured: {title}.",
    },
    # 経歴・学歴・研究キーワード・研究分野は記事化しない(ページに直接反映)
}

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATIC_IMG = ROOT / "static" / "img"
NEWS_JA = ROOT / "content" / "ja" / "news"
NEWS_EN = ROOT / "content" / "en" / "news"

API = "https://api.researchmap.jp/{}"
# 顔写真の自動同期。False にすると researchmap の写真は使わず、
# 自分で static/img/profile.jpg に置いた写真がそのまま使われ続ける
PHOTO_SYNC = False

AVATAR_CANDIDATES = [
    "https://researchmap.jp/{}/avatar.JPG",
    "https://researchmap.jp/{}/avatar.jpg",
    "https://researchmap.jp/{}/avatar.png",
    "https://researchmap.jp/{}/avatar.jpeg",
]

# ======================== 共通ユーティリティ ========================


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (site-sync)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (site-sync)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def strip_html(text):
    """researchmap のフィールドに混ざる HTML タグを除去してプレーンテキストにする。
    段落 (</p>) と改行 (<br>) は改行文字として残す。"""
    t = str(text)
    t = re.sub(r"(?i)</p\s*>|<br\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = htmllib.unescape(t)
    t = re.sub(r"[ \t　]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def ml(value):
    """researchmap の多言語フィールド {ja:..., en:...} を正規化。文字列ならそのまま両言語に。"""
    if value is None:
        return {"ja": "", "en": ""}
    if isinstance(value, str):
        v = strip_html(value)
        return {"ja": v, "en": v}
    if isinstance(value, dict):
        ja = value.get("ja") or value.get("en") or ""
        en = value.get("en") or value.get("ja") or ""
        return {"ja": strip_html(ja), "en": strip_html(en)}
    v = strip_html(value)
    return {"ja": v, "en": v}


def names(value):
    """authors 形式 {ja:[{name:...}], en:[...]} を 'A, B, C' の文字列に。"""
    out = {}
    for lang in ("ja", "en"):
        arr = []
        if isinstance(value, dict):
            arr = value.get(lang) or []
        if not arr and isinstance(value, list):
            arr = value
        parts = []
        for a in arr:
            if isinstance(a, dict):
                n = a.get("name")
                if isinstance(n, dict):
                    n = n.get(lang) or n.get("ja") or n.get("en")
                if n:
                    parts.append(str(n))
            elif isinstance(a, str):
                parts.append(a)
        out[lang] = ", ".join(parts)
    if not out.get("ja"):
        out["ja"] = out.get("en", "")
    if not out.get("en"):
        out["en"] = out.get("ja", "")
    return out


def year_of(datestr):
    m = re.match(r"(\d{4})", str(datestr or ""))
    return int(m.group(1)) if m else None


def slugify(text, maxlen=48):
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return (t or "item")[:maxlen].strip("-")


# ======================== 項目の正規化 ========================

SECTION_MAP = {
    "published_papers": "papers",
    "misc": "misc",
    "presentations": "presentations",
    "awards": "awards",
    "research_projects": "projects",
    "research_experience": "career",
    "education": "education",
    "committee_memberships": "committees",
    "association_memberships": "memberships",
    "teaching_experience": "teaching",
    "social_contribution": "social",
    "media_coverage": "media",
    "research_interests": "keywords",
    "research_areas": "fields",
}

# 種別ごとの「名称」フィールド(researchmap API実データで確認済み)
TYPE_TITLE = {
    "published_papers": ["paper_title"],
    "misc": ["paper_title", "misc_title", "title"],
    "presentations": ["presentation_title"],
    "awards": ["award_name", "title"],
    "research_projects": ["research_project_title"],
    "research_experience": ["affiliation"],
    "education": ["affiliation", "school_name"],
    "committee_memberships": ["committee_name"],
    "association_memberships": ["academic_society_name", "association"],
    "teaching_experience": ["subject_name", "subject"],
    "social_contribution": ["title", "event"],
    "media_coverage": ["title"],
}
# 種別ごとの「団体・掲載先」フィールド
TYPE_VENUE = {
    "published_papers": ["publication_name", "publisher"],
    "misc": ["publication_name", "publisher"],
    "presentations": ["event"],
    "awards": ["association", "awarder", "society", "organizer"],
    "research_projects": ["offer_organization"],
    "research_experience": [],
    "education": [],
    "committee_memberships": ["association", "organizer"],
    "association_memberships": [],
    "teaching_experience": ["institution_name", "institution"],
    "social_contribution": ["promoter", "organizer"],
    "media_coverage": ["publisher", "program_title", "media_name"],
}
TITLE_KEYS = [
    "paper_title", "presentation_title", "award_name", "research_project_title",
    "committee_name", "affiliation", "academic_society_name", "subject_name",
    "association", "subject", "title", "event", "keyword", "research_field",
]
VENUE_KEYS = [
    "publication_name", "event", "promoter", "organizer", "publisher",
    "association", "institution_name", "institution", "media_name",
    "program_title", "school_name", "awarder", "offer_organization",
]

# タイトル候補を全フィールドから探すときに除外するキー
NON_TITLE_KEYS = set(
    TITLE_KEYS[0:0]  # placeholder
) | {
    "from_date", "to_date", "publication_date", "award_date", "event_date",
    "start_date", "end_date", "date", "from_event_date", "to_event_date",
    "authors", "presenters", "winners", "investigators", "identifiers",
    "see_also", "referee", "invited", "languages", "@id", "@type", "rm:id",
    "rm:user_id", "display", "display_order", "presentation_type",
    "publication_type", "volume", "number", "starting_page", "ending_page",
    "grant_number", "national_grant_number", "overall_grant_amount",
    "description", "url", "doi", "country", "region", "job", "section",
    "department", "role", "publisher", "promoter", "organizer",
}


def is_ml_text(v):
    """多言語テキストらしい値か(文字列 or {ja/en}辞書)"""
    if isinstance(v, str):
        return not re.match(r"^\d{4}(-\d{2})?(-\d{2})?$", v) and not v.startswith("http")
    if isinstance(v, dict):
        return "ja" in v or "en" in v
    return False


def first_key(item, keys):
    for k in keys:
        if item.get(k):
            return item[k]
    return None


def guess_title(item):
    """TITLE_KEYSで見つからない場合、item内の最初のテキスト系フィールドをタイトルとみなす。"""
    for k, v in item.items():
        if k in NON_TITLE_KEYS or k in VENUE_KEYS:
            continue
        if is_ml_text(v):
            return v
    return None


def norm_item(rmtype, item, member):
    """API の item を、テンプレートが使う共通形式に落とす。"""
    raw_title = (first_key(item, TYPE_TITLE.get(rmtype, []))
                 or first_key(item, TITLE_KEYS) or guess_title(item))
    title = ml(raw_title)
    venue = ml(first_key(item, TYPE_VENUE.get(rmtype, []))
               if rmtype in TYPE_VENUE else first_key(item, VENUE_KEYS))
    if venue["ja"] == title["ja"] and venue["en"] == title["en"]:
        venue = {"ja": "", "en": ""}  # タイトルと同じ団体名は重複表示しない
    desc = ml(item.get("description") or item.get("job") or item.get("section") or item.get("department"))
    # 研究課題: 制度名・課題番号を詳細行に
    if rmtype == "research_projects":
        sysname = ml(item.get("system_name"))
        grant = str(item.get("grant_number") or item.get("national_grant_number") or "")
        for lang in ("ja", "en"):
            parts = [p for p in [sysname[lang], grant] if p]
            if parts:
                desc[lang] = " ".join(parts)
    authors = names(item.get("authors") or item.get("presenters") or item.get("winners") or item.get("investigators"))

    start = (item.get("from_date") or item.get("publication_date")
             or item.get("award_date") or item.get("event_date")
             or item.get("from_event_date")
             or item.get("start_date") or item.get("date") or "")
    end = item.get("to_date") or item.get("to_event_date") or item.get("end_date") or ""
    if str(end).startswith("9999"):
        end = ""  # researchmapの「継続中」表現 → 「現在」として表示
    if str(start).startswith("9999"):
        start = ""

    out = {
        "id": str(item.get("@id") or item.get("rm:id") or f"{rmtype}:{title['ja'] or title['en']}:{start}"),
        "member": member,
        "title": title,
        "venue": venue,
        "desc": desc,
        "authors": authors,
        "start": str(start),
        "end": str(end),
        "year": year_of(start),
        "peer_reviewed": bool(item.get("referee")),
        "invited": bool(item.get("invited")),
        "volume": ml(item.get("volume"))["en"] or ml(item.get("volume"))["ja"],
        "number": ml(item.get("number"))["en"] or ml(item.get("number"))["ja"],
        "page_start": str(item.get("starting_page") or ""),
        "page_end": str(item.get("ending_page") or ""),
        "doi": str(item.get("identifiers", {}).get("doi", [""])[0] if isinstance(item.get("identifiers", {}).get("doi"), list) else item.get("identifiers", {}).get("doi") or ""),
        "url": str(item.get("see_also", [{}])[0].get("@id", "") if isinstance(item.get("see_also"), list) and item.get("see_also") else ""),
    }
    # 経歴系: affiliation + section + job を結合した表示名を作る
    if rmtype in ("research_experience", "education", "committee_memberships",
                  "association_memberships", "teaching_experience"):
        for lang in ("ja", "en"):
            parts = [title[lang]]
            for extra_key in ("section", "department", "job", "committee_name"):
                v = ml(item.get(extra_key))[lang]
                if v and v not in parts:
                    parts.append(v)
            out["title"][lang] = " ".join(p for p in parts if p)
    return out


def sort_key(x):
    return x.get("start") or "0000"


def normalize(graph, member):
    """@graph 配列 → {papers: [...], misc: [...], ...}"""
    result = {}
    for sec in graph:
        rmtype = sec.get("@type")
        key = SECTION_MAP.get(rmtype)
        if not key:
            continue
        items = sec.get("items") or []
        if key == "keywords":
            result[key] = [ml(i.get("keyword") or i.get("title") or i) for i in items]
        elif key == "fields":
            result[key] = [ml(i.get("research_field") or i.get("title") or i) for i in items]
        else:
            # researchmapの表示と同じく日付の新しい順。日付が同じ/無い項目は元の並びを保つ
            result[key] = sorted(
                [norm_item(rmtype, i, member) for i in items],
                key=sort_key, reverse=True,
            )
    return result


def fetch_all_items(member, rmtype):
    """種別ごとのエンドポイントからページングで全件取得。失敗時は None。"""
    items, start = [], 1
    while True:
        try:
            j = fetch_json(f"{API.format(member)}/{rmtype}?limit=1000&start={start}")
        except Exception:
            return None if start == 1 else items
        batch = None
        if isinstance(j, dict):
            batch = j.get("items")
            if batch is None and "@graph" in j:
                g = j["@graph"]
                batch = (g[0].get("items") if g else []) or []
        elif isinstance(j, list):
            batch = j
        batch = batch or []
        items += batch
        if len(batch) < 1000:
            return items
        start += 1000


def build_profile(root_json, merged):
    """API ルートの基本情報 → data/profile.json"""
    fam, giv = ml(root_json.get("family_name")), ml(root_json.get("given_name"))
    fam_k = ml(root_json.get("family_name_kana"))
    giv_k = ml(root_json.get("given_name_kana"))
    profile = {
        "permalink": root_json.get("permalink") or PRIMARY,
        "name": {
            "ja": f"{fam['ja']} {giv['ja']}".strip() or f"{fam['en']} {giv['en']}".strip(),
            "en": f"{giv['en']} {fam['en']}".strip() or f"{giv['ja']} {fam['ja']}".strip(),
        },
        "kana": f"{fam_k['ja']} {giv_k['ja']}".strip(),
        "degree": "",
        "affiliation": {"ja": "", "en": ""},
        "job_title": {"ja": "", "en": ""},
        "bio": ml(root_json.get("profile")),
    }
    degrees = root_json.get("degrees") or []
    if degrees:
        d = degrees[0] if isinstance(degrees, list) else degrees
        profile["degree"] = ml(d.get("degree_name") if isinstance(d, dict) else d)["ja"]
    affs = root_json.get("affiliations") or []
    if affs:
        a = affs[0]
        profile["affiliation"] = ml(a.get("affiliation"))
        sec = ml(a.get("section"))
        for lang in ("ja", "en"):
            if sec[lang]:
                profile["affiliation"][lang] += " " + sec[lang]
        profile["job_title"] = ml(a.get("job"))
    # 現職は経歴の先頭からも補完できる
    if not profile["affiliation"]["ja"] and merged.get("career"):
        profile["affiliation"] = merged["career"][0]["title"]
    return profile


# ======================== お知らせ記事の生成 ========================


def news_body(rmtype, item, lang):
    tpl = NEWS_TEMPLATES[rmtype]
    title = item["title"][lang] or item["title"]["ja"] or item["title"]["en"]
    venue = item["venue"][lang] or item["venue"]["ja"] or item["venue"]["en"]
    if venue:
        return tpl[lang].format(title=title, venue=venue)
    return tpl.get(f"{lang}_novenue", tpl[lang]).format(title=title, venue="")


def write_news(rmtype, item):
    d = item["start"][:10] if item["start"] else date.today().isoformat()
    if len(d) == 4:
        d += "-01-01"
    elif len(d) == 7:
        d += "-01"
    slug = f"auto-{slugify(item['title']['en'] or item['title']['ja'])}"
    for lang, folder in (("ja", NEWS_JA), ("en", NEWS_EN)):
        body = news_body(rmtype, item, lang)
        title = body.split("。")[0] if lang == "ja" else body
        fm = "\n".join([
            "---",
            f"title: {json.dumps(body, ensure_ascii=False)}",
            f"date: {d}",
            "auto: true",
            f"rmtype: {rmtype}",
            "---",
        ])
        path = folder / f"{slug}.md"
        if not path.exists():
            path.write_text(fm + "\n", encoding="utf-8")


# ======================== メイン ========================


def main():
    merged = {}
    root_primary = None
    raw_samples = {}

    for member in MEMBERS:
        try:
            j = fetch_json(API.format(member))
        except Exception as e:
            print(f"[warn] {member}: API に到達できませんでした ({e})。今回は既存データのまま終了します。")
            return 0
        if member == PRIMARY:
            root_primary = j
        root_graph = j.get("@graph") or []

        # 種別ごとに全件取得(rootは件数上限で切れるため)。失敗した種別はrootの内容で代用
        full_graph = []
        for rmtype in SECTION_MAP:
            items = fetch_all_items(member, rmtype)
            if items is None:
                sec = next((s for s in root_graph if s.get("@type") == rmtype), None)
                items = (sec or {}).get("items") or []
            full_graph.append({"@type": rmtype, "items": items})
            if items and rmtype not in raw_samples:
                raw_samples[rmtype] = items[0]
            print(f"[info] {member}/{rmtype}: {len(items)} 件")

        data = normalize(full_graph, member)
        for k, v in data.items():
            if k in ("keywords", "fields"):
                merged.setdefault(k, [])
                seen_t = {x["ja"] for x in merged[k]}
                merged[k] += [x for x in v if x["ja"] not in seen_t]
            else:
                merged.setdefault(k, [])
                seen_ids = {x["id"] for x in merged[k]}
                merged[k] += [x for x in v if x["id"] not in seen_ids]

    # デバッグ用: 各種別の生データ1件を保存(表示不具合の調査に使う)
    try:
        (DATA / "rm_raw_sample.json").write_text(
            json.dumps(raw_samples, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    except Exception:
        pass

    # ---- お知らせの差分検出 ----
    seen_path = DATA / "rm_seen.json"
    first_run = not seen_path.exists()
    seen = set(json.loads(seen_path.read_text())) if not first_run else set()
    inv_map = {v: k for k, v in SECTION_MAP.items()}
    new_count = 0
    for key, items in merged.items():
        if key in ("keywords", "fields"):
            continue
        rmtype = inv_map.get(key)
        if rmtype not in NEWS_TEMPLATES:
            continue
        for item in items:
            if item["id"] not in seen:
                seen.add(item["id"])
                if not first_run:
                    write_news(rmtype, item)
                    new_count += 1
    if first_run:
        print("[info] 初回実行: 既存項目を既知として記録しました(お知らせ記事は作りません)。")
    else:
        print(f"[info] 新規項目 {new_count} 件をお知らせ記事にしました。")

    # ---- 顔写真 ----
    if not PHOTO_SYNC:
        avatar_candidates = []
        print("[info] 顔写真の自動同期はオフです(static/img/profile.jpg を手動管理)。")
    else:
        avatar_candidates = AVATAR_CANDIDATES
    for url in avatar_candidates:
        try:
            img = fetch_bytes(url.format(PRIMARY))
            if img and len(img) > 500:
                STATIC_IMG.mkdir(parents=True, exist_ok=True)
                (STATIC_IMG / "profile.jpg").write_bytes(img)
                print(f"[info] 顔写真を同期しました ({url.format(PRIMARY)})。")
                break
        except Exception:
            continue
    else:
        print("[warn] 顔写真が取得できませんでした。既存の写真を使います。")

    # ---- 書き出し ----
    DATA.mkdir(exist_ok=True)
    (DATA / "researchmap.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    if root_primary is not None:
        profile = build_profile(root_primary, merged)
        # X / LinkedIn はresearchmapにないので、既存ファイルの links を引き継ぐ
        prof_path = DATA / "profile.json"
        links = {"researchmap": f"https://researchmap.jp/{PRIMARY}", "orcid": "", "x": "", "linkedin": ""}
        if prof_path.exists():
            try:
                links.update(json.loads(prof_path.read_text()).get("links", {}))
            except Exception:
                pass
        profile["links"] = links
        prof_path.write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")
    seen_path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=0), encoding="utf-8")
    print("[info] data/researchmap.json / data/profile.json を更新しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
