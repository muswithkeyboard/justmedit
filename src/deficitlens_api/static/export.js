/* Justmedit — выгрузка отчёта в DOCX прямо в браузере.
   Без библиотек и без сервера: документ Word (Office Open XML) — это ZIP с несколькими XML-файлами; ZIP собираем
   «без сжатия» (метод STORE) и считаем CRC-32 сами. Отчёт никуда не отправляется и не сохраняется сервисом.
   Вход — простая модель документа: { title, footer, blocks: [{ t: "brand"|"meta"|"h1"|"h2"|"p"|"lead"|"li"|"small"|"table", ... }] };
   brand: { kind, date }; lead: { text, tone: "ok"|"anemia"|"urgent", tag }; ячейка таблицы — строка или { text, tone }. */
(function () {
  "use strict";

  // ---------- ZIP (STORE) ----------
  var CRC = (function () {
    var t = new Uint32Array(256);
    for (var n = 0; n < 256; n++) {
      var c = n;
      for (var k = 0; k < 8; k++) { c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1); }
      t[n] = c >>> 0;
    }
    return t;
  })();
  function crc32(bytes) {
    var c = 0xFFFFFFFF;
    for (var i = 0; i < bytes.length; i++) { c = CRC[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8); }
    return (c ^ 0xFFFFFFFF) >>> 0;
  }
  function zip(files) {                      // files: [{ name, data: Uint8Array }]
    var enc = new TextEncoder(), parts = [], central = [], offset = 0;
    var now = new Date();
    var dosTime = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);
    var dosDate = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
    files.forEach(function (f) {
      var name = enc.encode(f.name), crc = crc32(f.data), size = f.data.length;
      var h = new DataView(new ArrayBuffer(30));
      h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x0800, true); h.setUint16(8, 0, true);
      h.setUint16(10, dosTime, true); h.setUint16(12, dosDate, true); h.setUint32(14, crc, true);
      h.setUint32(18, size, true); h.setUint32(22, size, true); h.setUint16(26, name.length, true); h.setUint16(28, 0, true);
      parts.push(new Uint8Array(h.buffer), name, f.data);
      var c = new DataView(new ArrayBuffer(46));
      c.setUint32(0, 0x02014b50, true); c.setUint16(4, 20, true); c.setUint16(6, 20, true); c.setUint16(8, 0x0800, true);
      c.setUint16(10, 0, true); c.setUint16(12, dosTime, true); c.setUint16(14, dosDate, true); c.setUint32(16, crc, true);
      c.setUint32(20, size, true); c.setUint32(24, size, true); c.setUint16(28, name.length, true);
      c.setUint32(42, offset, true);
      central.push(new Uint8Array(c.buffer), name);
      offset += 30 + name.length + size;
    });
    var cdSize = central.reduce(function (s, p) { return s + p.length; }, 0);
    var e = new DataView(new ArrayBuffer(22));
    e.setUint32(0, 0x06054b50, true); e.setUint16(8, files.length, true); e.setUint16(10, files.length, true);
    e.setUint32(12, cdSize, true); e.setUint32(16, offset, true);
    return new Blob(parts.concat(central, [new Uint8Array(e.buffer)]),
      { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
  }

  // ---------- WordprocessingML ----------
  // Оформление — как на странице: тёмная шапка-таблица (ночной #061019, «Justmed» белым + «it» мятным, справа тип
  // отчёта и дата), строка «Пациент: …», вывод с цветной полосой по статусу, таблица с заливкой ячейки статуса,
  // заголовки разделов с полосой слева, нижний колонтитул с оговоркой и номером страницы (footer1.xml).
  var W = 9638;                                     // ширина текста A4 при полях 2 см, твипы
  var NIGHT = "061019", MINT = "2EF2D2", MINT2 = "19D3C5", MUTED = "55636B";
  var TONE_BAR = { ok: MINT2, anemia: "E0773A", urgent: "C0283E" };        // норма — мятный, анемия — оранжевый, срочно — красный
  var TONE_TAG = { ok: "08756B", anemia: "8A4B0E", urgent: "A3122A" };
  // Заливка ячейки статуса: ниже — светло-янтарный, выше — светло-розовый, норма — светло-мятный.
  var TONE_FILL = { low: "FFEFD2", high: "FDE2E8", normal: "E3FBF7", borderline: "FFF6DD" };
  var TONE_TEXT = { low: "8A4B0E", high: "A3122A", normal: "08756B", borderline: "8A4B0E" };
  var FOOTER = "Исследовательский прототип, не медицинское изделие. Решение принимает врач.";
  var NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' +
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"';

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function run(text, rpr) { return "<w:r>" + (rpr ? "<w:rPr>" + rpr + "</w:rPr>" : "") + '<w:t xml:space="preserve">' + esc(text) + "</w:t></w:r>"; }
  function para(text, style, rpr, ppr) {
    return "<w:p><w:pPr>" + (style ? '<w:pStyle w:val="' + style + '"/>' : "") + (ppr || "") + "</w:pPr>" + run(text, rpr) + "</w:p>";
  }
  // Свойства текста в порядке схемы OOXML: b, caps, color, sz.
  function rp(hex, sz, bold, caps) {
    return (bold ? "<w:b/>" : "") + (caps ? "<w:caps/>" : "") + '<w:color w:val="' + hex + '"/>' + (sz ? '<w:sz w:val="' + sz + '"/>' : "");
  }
  function field(instr, rpr) {                    // поле Word: PAGE / NUMPAGES (номер страницы в колонтитуле)
    var r = rpr ? "<w:rPr>" + rpr + "</w:rPr>" : "";
    return "<w:r>" + r + '<w:fldChar w:fldCharType="begin"/></w:r><w:r>' + r + '<w:instrText xml:space="preserve"> ' + instr + " </w:instrText></w:r>" +
      "<w:r>" + r + '<w:fldChar w:fldCharType="separate"/></w:r>' + run("1", rpr) + "<w:r>" + r + '<w:fldChar w:fldCharType="end"/></w:r>';
  }
  var NO_BORDERS = '<w:tblBorders><w:top w:val="nil"/><w:left w:val="nil"/><w:bottom w:val="nil"/><w:right w:val="nil"/>' +
    '<w:insideH w:val="nil"/><w:insideV w:val="nil"/></w:tblBorders>';

  // Шапка: таблица в одну строку на тёмном фоне.
  function brand(b) {
    var shd = '<w:shd w:val="clear" w:color="auto" w:fill="' + NIGHT + '"/><w:vAlign w:val="center"/>';
    var tight = '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>';
    var left = '<w:tc><w:tcPr><w:tcW w:w="5638" w:type="dxa"/>' + shd + "</w:tcPr>" +
      "<w:p><w:pPr>" + tight + "</w:pPr>" + run("Justmed", rp("FFFFFF", 40, true)) + run("it", rp(MINT, 40, true)) + "</w:p>" +
      "<w:p><w:pPr>" + tight + "</w:pPr>" + run("Анемии и скрытые дефициты по анализам крови", rp("9DB4BB", 16)) + "</w:p></w:tc>";
    var right = '<w:tc><w:tcPr><w:tcW w:w="4000" w:type="dxa"/>' + shd + "</w:tcPr>" +
      '<w:p><w:pPr>' + tight + '<w:jc w:val="right"/></w:pPr>' + run(b.kind, rp("FFFFFF", 22, true, true)) + "</w:p>" +
      '<w:p><w:pPr>' + tight + '<w:jc w:val="right"/></w:pPr>' + run(b.date || "", rp(MINT, 18)) + "</w:p></w:tc>";
    return '<w:tbl><w:tblPr><w:tblW w:w="' + W + '" w:type="dxa"/>' + NO_BORDERS +
      '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:top w:w="200" w:type="dxa"/><w:left w:w="260" w:type="dxa"/>' +
      '<w:bottom w:w="200" w:type="dxa"/><w:right w:w="260" w:type="dxa"/></w:tblCellMar></w:tblPr>' +
      '<w:tblGrid><w:gridCol w:w="5638"/><w:gridCol w:w="4000"/></w:tblGrid><w:tr>' + left + right + "</w:tr></w:tbl>";
  }
  // Вывод: таблица в одну ячейку на светлом фоне, слева — цветная полоса по статусу; внутри — метка и сам вывод.
  function lead(b) {
    var bar = TONE_BAR[b.tone] || MINT2;
    var tight = '<w:spacing w:before="0" w:after="0"/>';
    var inner = (b.tag ? para(b.tag, null, rp(TONE_TAG[b.tone] || MUTED, 16, true, true), '<w:spacing w:before="0" w:after="40"/>') : "") +
      para(b.text, "Lead", null, tight);
    return '<w:tbl><w:tblPr><w:tblW w:w="' + W + '" w:type="dxa"/>' + NO_BORDERS +
      '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:top w:w="150" w:type="dxa"/><w:left w:w="240" w:type="dxa"/>' +
      '<w:bottom w:w="170" w:type="dxa"/><w:right w:w="200" w:type="dxa"/></w:tblCellMar></w:tblPr>' +
      '<w:tblGrid><w:gridCol w:w="' + W + '"/></w:tblGrid><w:tr><w:trPr><w:cantSplit/></w:trPr><w:tc><w:tcPr><w:tcW w:w="' + W + '" w:type="dxa"/>' +
      '<w:tcBorders><w:left w:val="single" w:sz="36" w:space="0" w:color="' + bar + '"/></w:tcBorders>' +
      '<w:shd w:val="clear" w:color="auto" w:fill="F4F7F8"/></w:tcPr>' + inner + "</w:tc></w:tr></w:tbl>" +
      para("", null, null, '<w:spacing w:before="0" w:after="80"/>');
  }
  function cell(v, width, head) {
    var c = (v !== null && typeof v === "object") ? v : { text: v };
    var fill = head ? "F1F6F7" : TONE_FILL[c.tone];
    var rpr = head ? rp(MUTED, 18, true) : (TONE_TEXT[c.tone] ? rp(TONE_TEXT[c.tone], 20, true) : '<w:sz w:val="20"/>');
    return '<w:tc><w:tcPr><w:tcW w:w="' + width + '" w:type="dxa"/>' + (fill ? '<w:shd w:val="clear" w:color="auto" w:fill="' + fill + '"/>' : "") +
      '</w:tcPr><w:p><w:pPr><w:spacing w:before="50" w:after="50"/></w:pPr>' + run(c.text, rpr) + "</w:p></w:tc>";
  }
  function table(b) {
    var widths = b.widths || b.head.map(function () { return Math.floor(W / b.head.length); });
    var border = '<w:top w:val="single" w:sz="4" w:color="D5DEE2"/><w:bottom w:val="single" w:sz="4" w:color="D5DEE2"/>' +
      '<w:insideH w:val="single" w:sz="4" w:color="E4EAED"/>';
    var x = '<w:tbl><w:tblPr><w:tblW w:w="' + W + '" w:type="dxa"/><w:tblBorders>' + border + "</w:tblBorders>" +
      '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:left w:w="100" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr><w:tblGrid>' +
      widths.map(function (w) { return '<w:gridCol w:w="' + w + '"/>'; }).join("") + "</w:tblGrid>";
    x += '<w:tr><w:trPr><w:tblHeader/></w:trPr>' + b.head.map(function (h, i) { return cell(h, widths[i], true); }).join("") + "</w:tr>";
    b.rows.forEach(function (r) { x += '<w:tr><w:trPr><w:cantSplit/></w:trPr>' + r.map(function (v, i) { return cell(v, widths[i], false); }).join("") + "</w:tr>"; });
    return x + "</w:tbl>" + para("", null, null, '<w:spacing w:after="60"/>');
  }
  function body(model) {
    var x = "";
    model.blocks.forEach(function (b) {
      if (b.t === "brand") { x += brand(b); }
      else if (b.t === "meta") { x += para(b.text, "Meta"); }
      else if (b.t === "h1") { x += para(b.text, "Title"); }
      else if (b.t === "h2") { x += para(b.text, "Heading2"); }
      else if (b.t === "lead") { x += lead(b); }
      else if (b.t === "li") { x += para("•  " + b.text, "ListPara"); }
      else if (b.t === "small") { x += para(b.text, "Small"); }
      else if (b.t === "table") { x += table(b); }
      else { x += para(b.text, null); }
    });
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      "<w:document " + NS + "><w:body>" + x +
      '<w:sectPr><w:footerReference w:type="default" r:id="rId2"/><w:pgSz w:w="11906" w:h="16838"/>' +
      '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>' +
      "</w:body></w:document>";
  }
  // Нижний колонтитул: бренд и оговорка слева, «стр. N из M» справа (поля PAGE и NUMPAGES), тонкая линия сверху.
  function footer(text) {
    var small = rp(MUTED, 16);
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:ftr ' + NS + ">" +
      '<w:p><w:pPr><w:pBdr><w:top w:val="single" w:sz="4" w:space="6" w:color="D5DEE2"/></w:pBdr>' +
      '<w:tabs><w:tab w:val="right" w:pos="' + W + '"/></w:tabs><w:spacing w:before="0" w:after="0"/></w:pPr>' +
      run("Justmed", rp("0A1622", 16, true)) + run("it", rp("0A8F84", 16, true)) + run(" · " + (text || FOOTER), small) +
      "<w:r><w:tab/></w:r>" + run("стр. ", small) + field("PAGE", small) + run(" из ", small) + field("NUMPAGES", small) +
      "</w:p></w:ftr>";
  }
  var STYLES = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri" w:eastAsia="Calibri"/>' +
    '<w:color w:val="0A1622"/><w:sz w:val="22"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault>' +
    '<w:pPrDefault><w:pPr><w:spacing w:after="100" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>' +
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>' +
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="60"/></w:pPr>' +
    '<w:rPr><w:b/><w:color w:val="0A1622"/><w:sz w:val="40"/></w:rPr></w:style>' +
    '<w:style w:type="paragraph" w:styleId="Meta"><w:name w:val="Meta"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="140" w:after="60"/></w:pPr>' +
    '<w:rPr><w:color w:val="' + MUTED + '"/><w:sz w:val="19"/></w:rPr></w:style>' +
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>' +
    '<w:pPr><w:keepNext/><w:pBdr><w:left w:val="single" w:sz="24" w:space="8" w:color="' + MINT2 + '"/></w:pBdr>' +
    '<w:spacing w:before="300" w:after="100"/><w:ind w:left="180"/><w:outlineLvl w:val="1"/></w:pPr>' +
    '<w:rPr><w:b/><w:color w:val="0F4A52"/><w:sz w:val="26"/></w:rPr></w:style>' +
    '<w:style w:type="paragraph" w:styleId="Lead"><w:name w:val="Lead"/><w:basedOn w:val="Normal"/>' +
    '<w:pPr><w:spacing w:before="200" w:after="200"/></w:pPr>' +
    '<w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>' +
    '<w:style w:type="paragraph" w:styleId="ListPara"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>' +
    '<w:pPr><w:spacing w:after="60"/><w:ind w:left="360" w:hanging="240"/></w:pPr></w:style>' +
    '<w:style w:type="paragraph" w:styleId="Small"><w:name w:val="Small"/><w:basedOn w:val="Normal"/>' +
    '<w:rPr><w:color w:val="' + MUTED + '"/><w:sz w:val="18"/></w:rPr></w:style>' +
    "</w:styles>";
  var CT = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
    '<Default Extension="xml" ContentType="application/xml"/>' +
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' +
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>' +
    '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>' +
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>' +
    "</Types>";
  var RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' +
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>' +
    "</Relationships>";
  var DOC_RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' +
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>' +
    "</Relationships>";
  function core(title) {
    var iso = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" ' +
      'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" ' +
      'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' +
      "<dc:title>" + esc(title) + "</dc:title><dc:creator>Justmedit</dc:creator>" +
      '<dcterms:created xsi:type="dcterms:W3CDTF">' + iso + "</dcterms:created></cp:coreProperties>";
  }

  function docx(model) {
    var enc = new TextEncoder();
    return zip([
      { name: "[Content_Types].xml", data: enc.encode(CT) },
      { name: "_rels/.rels", data: enc.encode(RELS) },
      { name: "docProps/core.xml", data: enc.encode(core(model.title)) },
      { name: "word/document.xml", data: enc.encode(body(model)) },
      { name: "word/styles.xml", data: enc.encode(STYLES) },
      { name: "word/footer1.xml", data: enc.encode(footer(model.footer)) },
      { name: "word/_rels/document.xml.rels", data: enc.encode(DOC_RELS) }
    ]);
  }
  function download(blob, filename) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url; a.download = filename; a.rel = "noopener";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  window.JMExport = { docx: docx, download: download, _crc32: crc32 };
})();
