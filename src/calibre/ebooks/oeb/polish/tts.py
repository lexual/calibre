#!/usr/bin/env python
# License: GPLv3 Copyright: 2024, Kovid Goyal <kovid at kovidgoyal.net>

import json
from collections import defaultdict
from contextlib import suppress
from typing import NamedTuple

from lxml.etree import ElementBase as Element
from lxml.etree import tostring as _tostring

from calibre.ebooks.oeb.base import barename
from calibre.spell.break_iterator import sentence_positions
from calibre.utils.localization import canonicalize_lang, get_lang


class Sentence(NamedTuple):
    elem_id: str
    text: str
    lang: str
    voice : str


def tostring(x) -> str:
    return _tostring(x, encoding='unicode')


def lang_for_elem(elem, parent_lang):
    return canonicalize_lang(elem.get('lang') or elem.get('xml_lang') or elem.get('{http://www.w3.org/XML/1998/namespace}lang')) or parent_lang


def has_text(elem):
    if elem.text and elem.text.strip():
        return True
    for child in elem:
        if child.tail and child.tail.strip():
            return True
    return False


class Chunk(NamedTuple):
    child: Element | None
    text: str
    start_at: int
    is_tail: bool = False


continued_tag_names = frozenset({
    'a', 'span', 'em', 'strong', 'b', 'i', 'u', 'code', 'sub', 'sup', 'cite', 'q', 'kbd'
})
ignored_tag_names = frozenset({
    'img', 'object', 'script', 'style', 'head', 'title', 'form', 'input', 'br', 'hr', 'map', 'textarea', 'svg', 'math', 'rp', 'rt', 'rtc',
})


def mark_sentences_in_html(root, lang: str = '', voice: str = '') -> list[Sentence]:
    root_lang = canonicalize_lang(lang_for_elem(root, canonicalize_lang(lang or get_lang())) or 'en')
    root_voice = voice
    seen_ids = set(root.xpath('//*/@id'))
    id_counter = 1
    ans = []
    clones_map = defaultdict(list)

    class Parent:

        def __init__(self, elem, tag_name, parent_lang, parent_voice, child_lang=''):
            self.elem = elem
            self.tag_name = tag_name
            self.lang = child_lang or lang_for_elem(elem, parent_lang)
            q = elem.get('data-calibre-tts', '')
            self.voice = parent_voice
            if q.startswith('{'):  # }
                with suppress(Exception):
                    q = json.loads(q)
                    self.voice = q.get('voice') or parent_voice
            else:
                self.voice = q or parent_voice
            self.pos = 0
            self.texts = []
            if elem.text and elem.text.strip():
                self.texts.append(Chunk(None, elem.text, self.pos))
                self.pos += len(elem.text)
            self.children = tuple(elem.iterchildren())
            self.child_pos = 0

        def add_simple_child(self, elem):
            if text := elem.text:
                self.texts.append(Chunk(elem, text, self.pos))
                self.pos += len(text)

        def add_tail(self, elem, text):
            self.texts.append(Chunk(elem, text, self.pos, is_tail=True))
            self.pos += len(text)

        def commit(self) -> None:
            if not self.texts:
                return
            text = ''.join(c.text for c in self.texts)
            self.pos = 0
            for start, length in sentence_positions(text, self.lang):
                elem_id = self.wrap_sentence(start, length)
                ans.append(Sentence(elem_id, text[start:start+length], self.lang, self.voice))
            self.texts = []
            self.pos = 0

        def make_into_wrapper(self, elem: Element) -> str:
            nonlocal id_counter
            while True:
                q = f'cttsw-{id_counter}'
                if q not in seen_ids:
                    elem.set('id', q)
                    seen_ids.add(q)
                    return q
                id_counter += 1

        def make_wrapper(self, text: str | None) -> Element:
            ns, sep, _ = self.elem.tag.partition('}')
            ans = self.elem.makeelement(ns + sep + 'span')
            ans.text = text
            self.make_into_wrapper(ans)
            return ans

        def replace_reference_to_child(self, elem: Element, replacement: Element) -> None:
            for i in range(self.pos + 1, len(self.texts)):
                if self.texts[i].child is elem:
                    self.texts[i] = self.texts[i]._replace(child=replacement)
                else:
                    break

        def wrap_contents(self, first_child: Element | None, last_child: Element) -> Element:
            w = self.make_wrapper(self.elem.text if first_child is None else None)
            in_range = False
            for c in self.elem.iterchildren('*'):
                if not in_range and (first_child is None or first_child is c):
                    in_range = True
                    pos = self.elem.index(c)
                    self.elem.insert(pos, w)
                    w.append(c)
                    first_child = c
                if in_range:
                    if last_child is not first_child:
                        w.append(last_child)
                    if c is last_child:
                        break
            self.replace_reference_to_child(last_child, w)
            return w

        def clone_simple_element(self, elem: Element) -> Element:
            ans = elem.makeelement(elem.tag)
            ans.attrib.update(elem.attrib)
            ans.attrib.pop('id', None)
            ans.attrib.pop('name', None)
            ans.text, ans.tail = elem.text, elem.tail
            p = elem.getparent()
            idx = p.index(elem)
            p.insert(idx + 1, ans)
            self.replace_reference_to_child(elem, ans)
            clones_map[elem].append(ans)
            return ans

        def wrap_sentence(self, start: int, length: int) -> str:
            end = start + length
            start_chunk = end_chunk = -1
            start_offset = end_offset = 0
            for i in range(self.pos, len(self.texts)):
                c = self.texts[i]
                if c.start_at <= start:
                    start_chunk = i
                    start_offset = start - c.start_at
                if end <= c.start_at + len(c.text):
                    end_chunk = i
                    self.pos = i
                    end_offset = end - c.start_at
                    break
            else:
                self.pos = end_chunk = len(self.texts) - 1
                end_offset = len(self.texts[-1].text)
            assert start_chunk > -1
            s, e = self.texts[start_chunk], self.texts[end_chunk]
            if s.child is None: # start in leading text of parent element
                if e is s:  # end also in leading text of parent element
                    before, sentence, after = s.text[:start_offset], s.text[start_offset:end_offset], s.text[end_offset:]
                    self.elem.text = before
                    w = self.make_wrapper(sentence)
                    self.elem.insert(0, w)
                    w.tail = after
                    if after:
                        self.texts[self.pos] = Chunk(w, after, end, is_tail=True)
                    else:
                        self.pos += 1
                    return w.get('id')
                if e.is_tail:  # ending in the tail of a child
                    before_start, after_start = s.text[:start_offset], s.text[start_offset:]
                    included, after = e.text[:end_offset], e.text[end_offset:]
                    e.child.tail = included
                    self.elem.text = after_start
                    w = self.wrap_contents(None, e.child)
                    w.tail = after
                    self.elem.text = before_start
                    if after:
                        self.texts[self.pos] = Chunk(w, after, end, is_tail=True)
                    else:
                        self.pos += 1
                    return w.get('id')
                # ending inside a child
                before_start, after_start = s.text[:start_offset], s.text[start_offset:]
                included, after = e.text[:end_offset], e.text[end_offset:]
                e.child.text = included
                c = self.clone_simple_element(e.child)
                c.text = after
                e.child.tail = None
                self.elem.text = after_start
                w = self.wrap_contents(None, e.child)
                self.elem.text = before_start
                if after:
                    self.texts[self.pos] = Chunk(c, c.text, end)
                else:
                    self.pos += 1
                return w.get('id')
            # starting in a child text or tail
            if s.is_tail:
                if e.is_tail:
                    if s is e:  # end in tail of same element
                        before, sentence, after = s.text[:start_offset], s.text[start_offset:end_offset], s.text[end_offset:]
                        s.child.tail = before
                        w = self.make_wrapper(sentence)
                        w.tail = after
                        idx = self.elem.index(s.child)
                        self.elem.insert(idx + 1, w)
                        if after:
                            self.texts[self.pos] = Chunk(w, after, end, is_tail=True)
                        else:
                            self.pos += 1
                        return w.get('id')
                    s.child.tail, after_start = s.text[:start_offset], s.text[start_offset:]
                    e.child.tail, after_end = e.text[:end_offset], e.text[end_offset:]
                    idx = self.elem.index(s.child)
                    w = self.wrap_contents(self.elem[idx+1], e.child)
                    w.text, w.tail = after_start, after_end
                    if after_end:
                        self.texts[self.pos] = Chunk(w, after_end, end, is_tail=True)
                    else:
                        self.pos += 1
                    return w.get('id')
                # end inside some subsequent simple element
                s.child.tail, after_start = s.text[:start_offset], s.text[start_offset:]
                e.child.text, after_end = e.text[:end_offset], e.text[end_offset:]
                c = self.clone_simple_element(e.child)
                c.text = after_end
                e.child.tail = None
                w = self.wrap_contents(self.elem[self.elem.index(s.child) + 1], e.child)
                w.text = after_start
                if after_end:
                    self.texts[self.pos] = Chunk(c, after_end, end)
                else:
                    self.pos += 1
                return w.get('id')
            # start is in the text of a simple child
            if s.child is e.child:
                if e.is_tail:  # ending in tail of element we start in
                    before_start, after_start = s.text[:start_offset], s.text[start_offset:]
                    c = self.clone_simple_element(s.child)
                    s.child.text, s.child.tail = before_start, None
                    before_end, after_end = e.text[:end_offset], e.text[end_offset:]
                    c.text, c.tail = after_start, before_end
                    w = self.wrap_contents(c, c)
                    w.tail = after_end
                    if after_end:
                        self.texts[self.pos] = Chunk(w, after_end, end, is_tail=True)
                    else:
                        self.pos += 1
                    return w.get('id')
                # start and end in text of element
                before, sentence, after = s.text[:start_offset], s.text[start_offset:end_offset], s.text[end_offset:]
                c = self.clone_simple_element(s.child)
                s.child.text, s.child.tail = before, None
                c.text, c.tail = sentence, None
                c2 = self.clone_simple_element(c)
                c2.text = after
                self.make_into_wrapper(c)
                if after:
                    self.texts[self.pos] = Chunk(c2, after, end)
                else:
                    self.pos += 1
                return c.get('id')
            # end is in a subsequent simple child or tail of one
            s.child.text, after_start = s.text[:start_offset], s.text[start_offset:]
            c = self.clone_simple_element(s.child)
            c.text, s.child.tail = after_start, None
            if e.is_tail:
                e.child.tail, after_end = e.text[:end_offset], e.text[end_offset:]
                w = self.wrap_contents(c, e.child)
                w.tail = after_end
                if after_end:
                    self.texts[self.pos] = Chunk(w, after_end, end, is_tail=True)
                else:
                    self.pos += 1
                return w.get('id')
            # end is in text of subsequent simple child
            e.child.text, after_end = e.text[:end_offset], e.text[end_offset:]
            c2 = self.clone_simple_element(e.child)
            c2.text, e.child.tail = after_end, None
            w = self.wrap_contents(c, e.child)
            if after_end:
                self.texts[self.pos] = Chunk(c2, after_end, end)
            else:
                self.pos += 1
            return w.get('id')

    stack_of_parents = [Parent(elem, 'body', root_lang, root_voice) for elem in root.iterchildren('*') if barename(elem.tag).lower() == 'body']
    while stack_of_parents:
        p = stack_of_parents.pop()
        if len(p.elem) == 1 and not has_text(p.elem):  # wrapper
            c = p.elem[0]
            if isinstance(c.tag, str):
                stack_of_parents.append(Parent(c, barename(c.tag).lower(), p.lang, p.voice))
            continue
        for i in range(p.child_pos, len(p.children)):
            child = p.children[i]
            child_voice = child.get('data-calibre-tts', '')
            child_lang = lang_for_elem(child, p.lang)
            child_tag_name = barename(child.tag).lower() if isinstance(child.tag, str) else ''
            if child_lang == p.lang and child_voice == p.voice and child_tag_name in continued_tag_names and len(child) == 0:
                p.add_simple_child(child)
            elif child_tag_name not in ignored_tag_names:
                stack_of_parents.append(Parent(child, child_tag_name, p.lang, p.voice, child_lang=child_lang))
                p.commit()
                p.child_pos = i + 1
                stack_of_parents.append(p)
                continue
            if text := child.tail:
                p.add_tail(child, text)
        p.commit()
    for src_elem, clones in clones_map.items():
        for clone in clones + [src_elem]:
            if not clone.text and not clone.tail and not clone.get('id') and not clone.get('name'):
                if (p := clone.getparent()) is not None:
                    p.remove(clone)
    return ans