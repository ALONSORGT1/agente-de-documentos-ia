import test from 'node:test';
import assert from 'node:assert/strict';
import {renderAnswer} from '../assets/js/render.js';

class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_value) { throw new Error('HTML rendering is forbidden'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
}

function render(value) {
  const previous = globalThis.document;
  globalThis.document = {createElement: tag => new Node(tag)};
  try { const root = new Node('div'); renderAnswer(root, value); return root; }
  finally { globalThis.document = previous; }
}

function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }

test('model HTML and arbitrary links remain literal text without executable nodes', () => {
  const text = '<script>alert(1)</script> <img onerror=alert(1)> [fuente](javascript:alert(1)) [7]';
  const root = render(text);
  assert.equal(root.textContent, text);
  assert.ok(descendants(root).every(node => ['div', 'p', 'span'].includes(node.tagName)));
  assert.ok(descendants(root).every(node => !Object.hasOwn(node.attributes, 'href')));
});

test('answer headings keep the application hierarchy and format emphasis and code safely', () => {
  const root = render('# Resultado\n\nUn **dato** y *detalle* en `código <x>` [2].\n\n###### Subtítulo');
  assert.equal(root.children[0].tagName, 'h3');
  assert.equal(root.children[2].tagName, 'h6');
  assert.equal(descendants(root).find(node => node.tagName === 'strong').textContent, 'dato');
  assert.equal(descendants(root).find(node => node.tagName === 'em').textContent, 'detalle');
  assert.equal(descendants(root).find(node => node.tagName === 'code').textContent, 'código <x>');
  assert.match(root.textContent, /\[2\]/);
});

test('lists retain their supplied numbering, quotes and fenced code retain their contents', () => {
  const root = render('3. Primero\n4. Segundo\n\n- Uno\n- Dos\n\n> Extracto [1]\n> Continuación\n\n```html\n<script>test</script>\n```');
  assert.deepEqual(root.children.map(node => node.tagName), ['ol', 'ul', 'blockquote', 'pre']);
  assert.equal(root.children[0].attributes.start, '3');
  assert.equal(root.children[0].children.length, 2);
  assert.equal(root.children[2].textContent, 'Extracto [1]\nContinuación');
  assert.equal(root.children[3].children[0].textContent, '<script>test</script>');
});

test('tables have semantic column headers and a labelled keyboard-scrollable region', () => {
  const root = render('| Documento | Hallazgo |\n| --- | :---: |\n| **Manual** | Dato [1] |\n| Guía | a \\| b |');
  const wrapper = root.children[0];
  assert.equal(wrapper.className, 'answer-table-wrap');
  assert.equal(wrapper.attributes.tabindex, '0');
  assert.equal(wrapper.attributes.role, 'region');
  assert.equal(wrapper.attributes['aria-label'], 'Tabla de la respuesta');
  const table = wrapper.children[0];
  assert.equal(table.tagName, 'table');
  assert.equal(table.children[0].children[0].children[0].attributes.scope, 'col');
  assert.equal(table.children[1].children.length, 2);
  assert.equal(table.children[1].children[1].children[1].textContent, 'a | b');
});

test('unfinished fences and non-Markdown input render without dropping text or looping', () => {
  assert.equal(render('```\nno terminado').textContent, 'no terminado');
  assert.equal(render(null).children.length, 0);
  assert.equal(render('Solo texto').textContent, 'Solo texto');
  assert.equal(render('a | b\nnot a table\n\n---').children.at(-1).tagName, 'hr');
});
