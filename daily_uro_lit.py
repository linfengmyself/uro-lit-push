# -*- coding: utf-8 -*-
"""
每日泌尿外科文献英翻汉 · 微信推送
PubMed(按研究方向关键词+核心泌尿期刊) → 免费机翻(MyMemory) → Server酱微信推送
用法:
  python daily_uro_lit.py         # 正常执行(推送到微信)
  python daily_uro_lit.py --dry   # 试运行: 只生成预览不推送
配置: 与本文件同目录 config.json 中的 "sendkey" (Server酱 SendKey)
"""
import json
import os
import re
import sys
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py<3.9 兜底
    ZoneInfo = None

def bjnow():
    """北京时间 (消息日期/早晚标记统一按北京时间)"""
    if ZoneInfo is not None:
        return datetime.datetime.now(ZoneInfo("Asia/Shanghai"))
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "sent_state.json")
LOG_PATH = os.path.join(BASE, "run.log")
UA = {"User-Agent": "Mozilla/5.0 (daily-uro-lit/1.0; +self-use)"}

# ---------------------------------------------------------------- 基础工具
def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def http_post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=dict(UA, **{"Content-Type": "application/x-www-form-urlencoded"}))
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read()

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)

# ---------------------------------------------------------------- 1. 检索候选
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

def build_query(cfg):
    kw = cfg.get("keywords", [])
    jn = cfg.get("journals", [])
    parts = []
    for k in kw:
        parts.append('"%s"[Title/Abstract]' % k)
    for j in jn:
        parts.append('"%s"[Journal]' % j)
    days = int(cfg.get("days_window", 270))
    since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y/%m/%d")
    q = "(%s) AND English[lang] AND (%s[Date - Publication] : 3000[Date - Publication])" % (
        " OR ".join(parts), since)
    return q

def search_candidates(cfg):
    q = build_query(cfg)
    url = EUTILS + "/esearch.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "term": q, "retmax": str(cfg.get("retmax", 60)),
         "sort": "pub_date", "retmode": "json"})
    data = json.loads(http_get(url).decode())
    return data.get("esearchresult", {}).get("idlist", [])

def fetch_summaries(pids):
    """批量 esummary → {pid: {title, journal, date, doi, authors}}"""
    out = {}
    step = 80
    for i in range(0, len(pids), step):
        chunk = pids[i:i + step]
        url = EUTILS + "/esummary.fcgi?" + urllib.parse.urlencode(
            {"db": "pubmed", "id": ",".join(chunk), "retmode": "json"})
        res = json.loads(http_get(url).decode()).get("result", {})
        for pid in chunk:
            it = res.get(pid)
            if not it:
                continue
            auth = ""
            if it.get("authors"):
                a = it["authors"][0]
                auth = (a.get("name") or (a.get("lastname", "") + " " + a.get("initials", ""))).strip()
            out[pid] = {
                "title": it.get("title", "") or "",
                "journal": it.get("fulljournalname", "") or "",
                "date": str(it.get("pubdate", "") or ""),
                "doi": (it.get("articleids") and next((x["value"] for x in it["articleids"] if x.get("idtype") == "doi"), "") or ""),
                "author": auth,
            }
    return out

def fetch_abstract(pid):
    """efetch → (en_text, sections)"""
    url = EUTILS + "/efetch.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "id": pid, "retmode": "xml"})
    xml = http_get(url)
    root = ET.fromstring(xml)
    art = root.find(".//PubmedArticle")
    if art is None:
        return None
    title_el = art.find(".//ArticleTitle")
    title = "".join(title_el.itertext()).strip() if title_el is not None else ""
    sections = []
    for at in art.findall(".//Abstract/AbstractText"):
        label = (at.get("Label") or "").strip()
        text = "".join(at.itertext()).strip()
        if text:
            sections.append((label, text))
    if not sections:
        return None
    en_parts = []
    for label, text in sections:
        en_parts.append(("%s: %s" % (label.upper(), text)) if label else text)
    en_text = "\n".join(en_parts)
    return {"title": title, "en": en_text, "sections": sections}

# ---------------------------------------------------------------- 1.5 抽句段(面试练习用)
_PREF_LABEL = ["BACKGROUND AND OBJECTIVE", "BACKGROUND", "INTRODUCTION", "OBJECTIVE",
               "PURPOSE", "MATERIALS AND METHODS", "METHODS", "CONCLUSIONS AND CLINICAL IMPLICATIONS",
               "CONCLUSIONS", "CONCLUSION", "RESULTS", "KEY FINDINGS AND LIMITATIONS"]

def _split_sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(一-鿿])", text) if s.strip()]

def pick_passage(sections, min_w=55, max_w=150):
    """从摘要各小节按优先级抽出一段通顺句段, 供朗读+翻译练习 (返回 en文本, 词数)"""
    ordered = []
    for i, (label, text) in enumerate(sections):
        pri = _PREF_LABEL.index(label.upper()) if label.upper() in _PREF_LABEL else 99
        ordered.append((pri, i, text))
    ordered.sort(key=lambda x: (x[0], x[1]))
    sents = []
    for _, _, text in ordered:
        sents.extend(_split_sentences(text))
    # 贪婪选取直到达到 min_w, 最多 max_w
    chosen, n = [], 0
    for s in sents:
        w = len(s.split())
        if n + w > max_w and n >= min_w:
            break
        chosen.append(s)
        n += w
        if n >= min_w:
            break
    text = " ".join(chosen) if chosen else " ".join(sents)
    return text, len(text.split())

# ---------------------------------------------------------------- 2. 机翻
def split_chunks(text, limit=430):
    """按换行/句号切成 ≤limit 字符的小段"""
    units = []
    for para in re.split(r"\n+", text):
        para = para.strip()
        if not para:
            continue
        units.extend([s.strip() for s in re.split(r"(?<=[.;:!?])\s+", para) if s.strip()])
    chunks, cur = [], ""
    for u in units:
        if len(u) > limit:  # 超长单句直接截断
            u = u[:limit]
        if len(cur) + len(u) + 1 > limit:
            chunks.append(cur)
            cur = u
        else:
            cur = cur + " " + u if cur else u
    if cur:
        chunks.append(cur)
    return chunks

def translate(text, langpair):
    """MyMemory 免费机翻; 返回 (ok, cn_text)"""
    chunks = split_chunks(text)
    if not chunks:
        return True, ""
    out = []
    total = 0
    for c in chunks:
        total += len(c)
        if total > 4700:  # 免费额度 ~5000 字符/天，留余量
            break
        q = urllib.parse.quote(c)
        url = "https://api.mymemory.translated.net/get?q=%s&langpair=%s" % (q, urllib.parse.quote(langpair))
        try:
            body = json.loads(http_get(url, timeout=30).decode())
        except Exception as e:
            log("MT error: %s" % e)
            return False, " ".join(out)
        txt = (body.get("responseData") or {}).get("translatedText") or ""
        if txt.startswith("MYMEMORY WARNING") or body.get("responseStatus") != 200:
            log("MT quota/limit: %s" % txt[:80])
            return False, " ".join(out)
        out.append(txt.strip())
        time.sleep(0.5)
    return True, " ".join(out)

def push_wechat(cfg, title, desp):
    # 优先取环境变量 SENDKEY(GitHub Actions Secret), 其次 config.json
    key = (os.environ.get("SENDKEY") or cfg.get("sendkey") or "").strip()
    if not key:
        log("no sendkey configured")
        return False
    url = "https://sctapi.ftqq.com/%s.send" % key
    try:
        body = http_post(url, {"title": title[:32], "desp": desp})
        resp = json.loads(body.decode())
        if resp.get("code") == 0 or resp.get("errno") == 0:
            log("pushed OK, pushid=%s" % resp.get("data", {}).get("pushid", "?"))
            return True
        log("push failed: %s" % str(resp)[:300])
        return False
    except Exception as e:
        log("push exception: %s" % e)
        return False

# ---------------------------------------------------------------- 主流程
def main():
    dry = "--dry" in sys.argv
    cfg = load_json(CFG_PATH, None)
    if not cfg:
        log("config.json missing")
        return
    sent = load_json(STATE_PATH, {"pushed": []})
    pushed = set(sent.get("pushed", []))

    pids = search_candidates(cfg)
    log("candidates: %d" % len(pids))
    if not pids:
        log("no candidates found")
        return
    metas = fetch_summaries(pids)

    chosen = None
    for pid in pids:
        if pid in pushed:
            continue
        meta = metas.get(pid)
        if not meta or not meta["title"]:
            continue
        try:
            paper = fetch_abstract(pid)
        except Exception as e:
            log("fetch abstract %s error: %s" % (pid, e))
            continue
        if paper and len(paper["en"]) >= 300:
            chosen = (pid, meta, paper)
            break
    if not chosen:
        log("nothing new to push")
        return

    pid, meta, paper = chosen
    meta["pid"] = pid
    log("chosen PMID %s: %s" % (pid, meta["title"][:80]))

    # ---- 面试翻译练习: 抽一小段而非整篇 ----
    passage, wc = pick_passage(paper["sections"])
    ok, cn = translate(passage, cfg.get("langpair", "en|zh-CN"))
    if not ok:
        log("translation incomplete (quota?)")

    year = re.match(r"\d{4}", meta["date"])
    year = year.group(0) if year else ""
    now_bj = bjnow()
    part = "早" if now_bj.hour < 14 else "晚"
    title = "泌尿翻译练习·%s篇 %s" % (part, now_bj.strftime("%m-%d"))
    cn_show = cn or "（机翻失败，请查看原文）"
    jour_show = ("%s, %s" % (meta["journal"], year)) if year else meta["journal"]
    desp = "\n\n".join([
        "**面试翻译练习 · %s篇（约 %d 词）**" % (part, wc),
        "> 练习方式：先朗读英文 1 遍 → 口头翻译 → 再看下方参考译文对照",
        "### 🔤 英文段落",
        passage,
        "### 🇨🇳 中文参考翻译（机翻）",
        cn_show,
        "### 📎 出处",
        "%s · %s · PMID %s" % (meta["title"], jour_show, pid),
        "[PubMed 原文链接](https://pubmed.ncbi.nlm.nih.gov/%s/)" % pid,
    ])
    desp += "\n\n---\n> 每日自动推送 · 仅一段供面试朗读/翻译练习，机翻仅供参考"

    if dry:
        with open(os.path.join(BASE, "preview.md"), "w", encoding="utf-8") as f:
            f.write("# " + title + "\n\n" + desp)
        log("DRY preview written: preview.md (PMID %s, passage words %d, cn chars %d)" % (pid, wc, len(cn)))
        return

    if push_wechat(cfg, title, desp):
        pushed.add(pid)
        sent["pushed"] = sorted(pushed)[-400:]
        sent.setdefault("last", {})["pid"] = pid
        sent["last"]["date"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        save_json(STATE_PATH, sent)
        log("state saved")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL: %s" % e)
