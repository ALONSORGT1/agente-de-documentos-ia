// Deliberately small Markdown renderer. Every model-provided character enters
// the DOM through textContent; HTML, links and citation markers stay inert.
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function inline(parent, value) {
  const tokens = /(`+)([^`\n]+?)\1|\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*|_([^_\n]+)_/g;
  let position = 0;
  for (const match of value.matchAll(tokens)) {
    if (match.index > position) parent.append(element('span', value.slice(position, match.index)));
    if (match[1]) parent.append(element('code', match[2]));
    else if (match[3] || match[4]) parent.append(element('strong', match[3] || match[4]));
    else parent.append(element('em', match[5] || match[6]));
    position = match.index + match[0].length;
  }
  if (position < value.length) parent.append(element('span', value.slice(position)));
}

function cells(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
}

function tableStart(lines, index) {
  if (!lines[index]?.includes('|') || !lines[index + 1]?.includes('|')) return false;
  const header = cells(lines[index]);
  const separator = cells(lines[index + 1]);
  return header.length > 1 && header.length === separator.length && separator.every(cell => /^:?-{3,}:?$/.test(cell));
}

function listItem(line) {
  return /^\s{0,3}(?:(\d+)[.)]|([-+*]))\s+(.+)$/.exec(line);
}

function blockStart(lines, index) {
  const line = lines[index] || '';
  return /^\s*$/.test(line) || /^\s{0,3}(?:#{1,6}\s|`{3,}|~{3,}|>\s?)/.test(line) || Boolean(listItem(line)) || tableStart(lines, index) || /^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line);
}

/** Render the supported document formatting without interpreting HTML or URLs. */
export function renderAnswer(container, value) {
  const text = typeof value === 'string' ? value : '';
  container.replaceChildren();
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index++; continue; }

    const fence = /^\s{0,3}(`{3,}|~{3,})(.*)$/.exec(line);
    if (fence) {
      const code = [];
      const close = new RegExp(`^\\s{0,3}${fence[1][0]}{${fence[1].length},}\\s*$`);
      index++;
      while (index < lines.length && !close.test(lines[index])) code.push(lines[index++]);
      if (index < lines.length) index++;
      const pre = element('pre', undefined, 'answer-code');
      pre.append(element('code', code.join('\n')));
      container.append(pre);
      continue;
    }

    const heading = /^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) {
      const node = element(`h${Math.min(6, heading[1].length + 2)}`);
      inline(node, heading[2]);
      container.append(node);
      index++;
      continue;
    }

    if (tableStart(lines, index)) {
      const header = cells(line);
      const wrapper = element('div', undefined, 'answer-table-wrap');
      wrapper.setAttribute('tabindex', '0');
      wrapper.setAttribute('role', 'region');
      wrapper.setAttribute('aria-label', 'Tabla de la respuesta');
      const table = element('table', undefined, 'answer-table');
      const head = element('thead');
      const headRow = element('tr');
      for (const title of header) {
        const cell = element('th');
        cell.setAttribute('scope', 'col');
        inline(cell, title);
        headRow.append(cell);
      }
      head.append(headRow);
      table.append(head);
      const body = element('tbody');
      index += 2;
      while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
        const values = cells(lines[index]);
        if (values.length !== header.length) break;
        const row = element('tr');
        for (const value of values) {
          const cell = element('td');
          inline(cell, value);
          row.append(cell);
        }
        body.append(row);
        index++;
      }
      table.append(body);
      wrapper.append(table);
      container.append(wrapper);
      continue;
    }

    const firstItem = listItem(line);
    if (firstItem) {
      const ordered = Boolean(firstItem[1]);
      const list = element(ordered ? 'ol' : 'ul');
      if (ordered && Number(firstItem[1]) !== 1) list.setAttribute('start', firstItem[1]);
      while (index < lines.length) {
        const match = listItem(lines[index]);
        if (!match || Boolean(match[1]) !== ordered) break;
        const item = element('li');
        inline(item, match[3]);
        list.append(item);
        index++;
      }
      container.append(list);
      continue;
    }

    if (/^\s{0,3}>/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s{0,3}>/.test(lines[index])) quote.push(lines[index++].replace(/^\s{0,3}>\s?/, ''));
      const node = element('blockquote');
      inline(node, quote.join('\n'));
      container.append(node);
      continue;
    }

    if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      container.append(element('hr'));
      index++;
      continue;
    }

    const paragraph = [line];
    index++;
    while (index < lines.length && !blockStart(lines, index)) paragraph.push(lines[index++]);
    const node = element('p');
    inline(node, paragraph.join('\n'));
    container.append(node);
  }
}
