/*
 * obsurl.js — OBSブラウザソース用URLの一覧（コックピット / アプリ設定で共用）
 *
 * 「全部入り」「文字起こしのみ」「翻訳1〜3」を並べ、行ごとに背景色（透過 or HEX）を
 * 選ぶとURL（?part=...&bg=RRGGBB）へ即反映する。選択は保存しない（URLに載るだけ）。
 *
 *   ObsUrl.render(container, cfg, { onCopy })
 */
(function () {
  const T = (key, jaDefault) =>
    window.MojicastI18n ? window.MojicastI18n.t(key, jaDefault) : jaDefault;

  // 翻訳先コード → 表示名（アプリ設定の翻訳先セレクトと同じ訳語キーを使う）
  const LANG_NAMES = {
    en: "英語", zh: "中国語（簡体字・中国大陸）", zh_tw: "中国語（繁体字・台湾）",
    zh_hk: "中国語（繁体字・香港）", id: "インドネシア語", ja: "日本語",
    ko: "韓国語（試験的）",
  };
  const langName = (code) =>
    T("st.tl_" + code.replace("_", ""), LANG_NAMES[code] || code);

  /** 表示する要素の行定義（翻訳は設定されているぶんだけ） */
  function partRows(cfg) {
    const rows = [{ part: "", label: T("obs.part_all", "全部入り") },
                  { part: "asr", label: T("obs.part_asr", "文字起こしのみ") }];
    if (cfg && cfg.translate)
      ["translate_lang", "translate_lang2", "translate_lang3"].forEach((key, i) => {
        const code = (cfg[key] || "").trim();
        if (!code) return;
        rows.push({ part: "tr" + (i + 1),
                    label: T("obs.part_tr", "翻訳{n}（{lang}）")
                      .replace("{n}", i + 1).replace("{lang}", langName(code)) });
      });
    return rows;
  }

  function makeRow(row, opts) {
    const el = document.createElement("div");
    el.className = "obsrow";
    el.innerHTML =
      '<div class="obsrow-top"><span class="obsrow-label"></span>' +
      '<label class="obsrow-bg"><input type="checkbox" checked><span></span></label>' +
      '<input type="color" value="#000000" disabled></div>' +
      '<div class="obsrow-url"><input type="text" readonly><button></button></div>';
    const [transparent, color, url] = el.querySelectorAll("input");
    const btn = el.querySelector("button");
    el.querySelector(".obsrow-label").textContent = row.label;
    el.querySelector(".obsrow-bg span").textContent = T("obs.transparent", "透過");
    color.title = T("obs.bg_color", "背景色");
    btn.textContent = T("cp.copy", "コピー");

    const sync = () => {
      color.disabled = transparent.checked;
      const q = [];
      if (row.part) q.push("part=" + row.part);
      if (!transparent.checked) q.push("bg=" + color.value.replace("#", ""));
      url.value = location.origin + "/" + (q.length ? "?" + q.join("&") : "");
    };
    transparent.addEventListener("change", sync);
    color.addEventListener("input", sync);
    btn.addEventListener("click", () => {
      url.select();
      try { navigator.clipboard.writeText(url.value); }
      catch (e) { document.execCommand("copy"); }
      if (opts && opts.onCopy) opts.onCopy(url.value);
    });
    sync();
    return el;
  }

  function render(container, cfg, opts) {
    if (!container) return;
    container.textContent = "";
    for (const row of partRows(cfg)) container.appendChild(makeRow(row, opts));
  }

  window.ObsUrl = { render };
})();
