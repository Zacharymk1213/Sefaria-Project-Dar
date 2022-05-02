import dataclasses
from collections import defaultdict
from typing import List, Union, Dict, Optional, Tuple, Generator, Iterable, Set
from enum import Enum
from functools import reduce
from itertools import product
from sefaria.system.exceptions import InputError
from . import abstract as abst
from . import text
from . import schema
import spacy
from tqdm import tqdm
from spacy.tokens import Span, Token
from spacy.language import Language

spacy.prefer_gpu()

# keys correspond named entity labels in spacy models
# values are properties in RefPartType
LABEL_TO_REF_PART_TYPE_ATTR = {
    # HE
    "כותרת": 'NAMED',
    "מספר": "NUMBERED",
    "דה": "DH",
    "סימן-טווח": "RANGE_SYMBOL",
    "לקמן-להלן": "RELATIVE",
    "שם": "IBID",
    "לא-רציף": "NON_CTS",
    # EN
    "title": 'NAMED',
    "number": "NUMBERED",
    "DH": "DH",
    "range-symbol": "RANGE_SYMBOL",
    "dir-ibid": "RELATIVE",
    "ibid": "IBID",
    "non-cts": "NON_CTS",
}

SpanOrToken = Union[Span, Token]  # convenience type since Spans and Tokens are very similar


def span_inds(span: SpanOrToken) -> Tuple[int, int]:
    """
    For some reason, spacy makes it difficult to deal with indices in tokens and spans
    These classes use different fields for their indices
    This function unifies access to indices
    """
    start = span.start if isinstance(span, Span) else span.i
    end = span.end if isinstance(span, Span) else (span.i+1)
    return start, end


def span_char_inds(span: SpanOrToken) -> Tuple[int, int]:
    if isinstance(span, Span):
        return span.start_char, span.end_char
    elif isinstance(span, Token):
        idx = span.idx
        return idx, idx + len(span)


class RefPartType(Enum):
    NAMED = "named"
    NUMBERED = "numbered"
    DH = "dibur_hamatchil"
    RANGE_SYMBOL = "range_symbol"
    RANGE = "range"
    RELATIVE = "relative"
    IBID = "ibid"
    NON_CTS = "non_cts"

    @classmethod
    def span_label_to_enum(cls, span_label: str) -> 'RefPartType':
        """
        Convert span label from spacy named entity to RefPartType
        """
        return getattr(cls, LABEL_TO_REF_PART_TYPE_ATTR[span_label])


class ContextType(Enum):
    """
    Types of context which can be used to help resolve refs
    """
    CURRENT_BOOK = "CURRENT_BOOK"
    IBID = "IBID"


# maps ContextTypes that will always (I believe) map to certain RefPartTypes
# they don't necessarily need to map to any RefPart but if they do, they will match these types
CONTEXT_TO_REF_PART_TYPE = {
    ContextType.CURRENT_BOOK: {RefPartType.RELATIVE},
    ContextType.IBID: {RefPartType.IBID}
}


class TrieEntry:
    """
    Base class for entries in MatchTemplateTrie
    """
    key_is_id = False  # is key an ID which shouldn't be manipulated with string functions?

    def key(self):
        return hash(self)


class LeafTrieEntry:
    pass


# static entry which represents a leaf entry in MatchTemplateTrie
LEAF_TRIE_ENTRY = LeafTrieEntry()


class NonUniqueTerm(abst.SluggedAbstractMongoRecord, schema.AbstractTitledObject):
    """
    The successor of the old `Term` class
    Doesn't require titles to be globally unique
    """
    cacheable = True
    collection = "non_unique_terms"
    required_attrs = [
        "slug",
        "titles"
    ]
    optional_attrs = [
        "ref_part_role",  # currently either "structural", "context_swap" or "alt_title". structural should be used for terms that used to define a logical relationship between ref parts (e.g. 'yerushalmi'). "alt_title" is for parts that are only included to generate more alt_titles (e.g. 'sefer'). "context_swap" is for parts that are meant to be swapped via SchemaNode.ref_resolver_context_swaps
    ]
    slug_fields = ['slug']
    title_group = None
    
    def _normalize(self):
        super()._normalize()
        self.titles = self.title_group.titles

    def set_titles(self, titles):
        self.title_group = schema.TitleGroup(titles)

    def _set_derived_attributes(self):
        self.set_titles(getattr(self, "titles", None))

    def __repr__(self):
        return f'{self.__class__.__name__}.init("{self.slug}")'

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__hash__() == other.__hash__()

    def __hash__(self):
        return hash(self.slug)

    def __ne__(self, other):
        return not self.__eq__(other)


class NonUniqueTermSet(abst.AbstractMongoSet):
    recordClass = NonUniqueTerm


class MatchTemplate:
    """
    Template for matching a SchemaNode to a RawRef
    """
    def __init__(self, term_slugs, scope='combined'):
        self.term_slugs = term_slugs
        self.scope = scope

    def get_terms(self) -> Iterable[NonUniqueTerm]:
        for slug in self.term_slugs:
            yield NonUniqueTerm.init(slug)

    terms = property(get_terms)


class RawRefPart(TrieEntry):
    """
    Immutable part of a RawRef
    Represents a unit of text used to find a match to a SchemaNode
    """
    key_is_id = False
    max_dh_continuation_len = 4  # max num tokens in potential_dh_continuation. more likely doesn't add more information

    def __init__(self, type: RefPartType, span: Optional[SpanOrToken], potential_dh_continuation: SpanOrToken = None) -> None:
        self.span = span
        self.type = type
        if potential_dh_continuation is not None:
            if isinstance(potential_dh_continuation, Span) and len(potential_dh_continuation) > self.max_dh_continuation_len:
                potential_dh_continuation = potential_dh_continuation[:self.max_dh_continuation_len]
        self.potential_dh_continuation = potential_dh_continuation

    def __str__(self):
        return f"{self.__class__.__name__}: {self.span}, {self.type}"

    def __repr__(self):
        return f"{self.__class__.__name__}({self.span}, {self.dh_cont_text})"

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__hash__() == other.__hash__()

    def __hash__(self):
        return hash(f"{self.type}|{self.span.__hash__()}|{self.dh_cont_text}")

    def __ne__(self, other):
        return not self.__eq__(other)

    def key(self):
        return self.text

    @property
    def text(self):
        return self.span.text

    @property
    def dh_cont_text(self):
        return '' if self.potential_dh_continuation is None else self.potential_dh_continuation.text

    def get_dh_text_to_match(self) -> Iterable[str]:
        import re
        m = re.match(r'^(?:(?:\u05d1)?(?:\u05d3"\u05d4|s ?\. ?v ?\.) )?(.+?)$', self.text)
        if m is not None:
            dh = m.group(1)
            if self.potential_dh_continuation:
                for i in range(len(self.potential_dh_continuation), 0, -1):
                    yield f"{dh} {self.potential_dh_continuation[:i]}"
            # no matter what yield just the dh
            yield dh

    @property
    def is_context(self):
        return isinstance(self, ContextPart)


class ContextPart(RawRefPart):
    # currently used to easily differentiate TermContext and SectionContext from a vanilla RawRefPart
    pass


class TermContext(ContextPart):
    """
    Represents context backed by a NonUniqueTerm
    """
    key_is_id = True

    def __init__(self, term: NonUniqueTerm):
        super().__init__(RefPartType.NAMED, None)
        self.term = term

    def key(self):
        return self.__repr__()

    @property
    def text(self):
        return self.__str__()

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        return f"{self.__class__.__name__}({repr(self.term)})"

    def __hash__(self):
        return hash(self.__repr__())


class SectionContext(ContextPart):
    """
    Represents a section in a context ref
    Used for injecting section context into a match which is missing sections (e.g. 'Tosafot on Berakhot DH abcd' is missing a daf)
    NOTE: used to used index of section to help validate. Doesn't work b/c we change sections list on the nodes as we refine them
    """

    def __init__(self, addr_type: schema.AddressType, section_name: str, address: int) -> None:
        """
        :param addr_type: AddressType of section
        :param section_name: Name of section
        :param address: Actual address, to be interpreted by `addr_type`
        """
        super().__init__(RefPartType.NUMBERED, None)
        self.addr_type = addr_type
        self.section_name = section_name
        self.address = address

    @property
    def text(self):
        return self.__str__()

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        addr_name = self.addr_type.__class__.__name__
        return f"{self.__class__.__name__}({addr_name}(0), '{self.section_name}', {self.address})"

    def __hash__(self):
        return hash(f"{self.addr_type.__class__}|{self.section_name}|{self.address}")


class RangedRawRefParts(RawRefPart):
    """
    Container for ref parts that represent the sections and toSections of a ranged ref
    """
    def __init__(self, sections: List[RawRefPart], toSections: List[RawRefPart]):
        super().__init__(RefPartType.RANGE, self._get_full_span(sections, toSections))
        self.sections = sections
        self.toSections = toSections

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__hash__() == other.__hash__()

    def __hash__(self):
        return hash(hash(p) for p in (self.sections + self.toSections))

    def __ne__(self, other):
        return not self.__eq__(other)

    @staticmethod
    def _get_full_span(sections, toSections):
        start_span = sections[0].span
        start_token_i = span_inds(start_span)[0]
        end_token_i = span_inds(toSections[-1].span)[1]
        return start_span.doc[start_token_i:end_token_i]


class RawRef:
    """
    Span of text which may represent one or more Refs
    Contains RawRefParts
    """
    def __init__(self, raw_ref_parts: list, span: SpanOrToken) -> None:
        self.raw_ref_parts = self._group_ranged_parts(raw_ref_parts)
        self.parts_to_match = self.raw_ref_parts  # actual parts that will be matched. different when their are context swaps
        self.prev_num_parts_map = self._get_prev_num_parts_map(self.raw_ref_parts)
        self.span = span

    @staticmethod
    def _group_ranged_parts(raw_ref_parts: List['RawRefPart']) -> List['RawRefPart']:
        """
        Preprocessing function to group together RawRefParts which represent ranged sections
        """
        ranged_symbol_ind = None
        for i, part in enumerate(raw_ref_parts):
            if part.type == RefPartType.RANGE_SYMBOL:
                ranged_symbol_ind = i
                break
        if ranged_symbol_ind is None or ranged_symbol_ind == len(raw_ref_parts) - 1: return raw_ref_parts
        section_slice, toSection_slice = None, None
        for i in range(ranged_symbol_ind-1, -1, -1):
            if i == 0 or raw_ref_parts[i-1].type != RefPartType.NUMBERED:
                section_slice = slice(i, ranged_symbol_ind)
                break
        for i in range(ranged_symbol_ind+1, len(raw_ref_parts)):
            if i == len(raw_ref_parts) - 1 or raw_ref_parts[i+1].type != RefPartType.NUMBERED:
                toSection_slice = slice(ranged_symbol_ind+1, i+1)
                break
        if section_slice is None: return raw_ref_parts
        sections = raw_ref_parts[section_slice]
        toSections = sections[:]
        num_explicit_to_sections = toSection_slice.stop - toSection_slice.start
        toSections[-num_explicit_to_sections:] = raw_ref_parts[toSection_slice]
        new_raw_ref_parts = raw_ref_parts[:section_slice.start] + \
                            [RangedRawRefParts(sections, toSections)] + \
                            raw_ref_parts[toSection_slice.stop:]
        return new_raw_ref_parts

    @staticmethod
    def _get_prev_num_parts_map(raw_ref_parts: List[RawRefPart]) -> Dict[RawRefPart, RawRefPart]:
        """
        Helper function to avoid matching NUMBERED RawRefParts that match AddressInteger sections out of order
        AddressInteger sections must resolve in order because resolving out of order would be meaningless
        Returns a map from NUMBERED RawRefParts to directly preceeding NUMBERED RawRefParts
        """
        if len(raw_ref_parts) == 0: return {}
        prev_num_parts_map = {}
        prev_part = raw_ref_parts[0]
        for part in raw_ref_parts[1:]:
            if prev_part.type == RefPartType.NUMBERED and part.type == RefPartType.NUMBERED:
                prev_num_parts_map[part] = prev_part
            prev_part = part
        return prev_num_parts_map

    def subspan(self, part_slice: slice) -> SpanOrToken:
        """
        Return subspan covered by `part_slice`, relative to self.span
        """
        parts = self.raw_ref_parts[part_slice]
        start_token_i = span_inds(parts[0].span)[0]
        end_token_i = span_inds(parts[-1].span)[1]

        offset_i = span_inds(self.span)[0]
        subspan = self.span.doc[offset_i+start_token_i:offset_i+end_token_i]
        # unfortunately, the two models were trained using different tokenizers leading to potential differences in token indexes
        assert subspan.text == parts[0].span.doc[start_token_i:end_token_i].text, f"{subspan.text} != {parts[0].span.doc[start_token_i:end_token_i].text}"
        return subspan

    @property
    def text(self):
        """
        Return text of underlying span
        """
        return self.span.text

    @property
    def char_indices(self) -> Tuple[int, int]:
        """
        Return start and end char indices of underlying text
        """
        return span_char_inds(self.span)


class ResolvedRef:
    """
    Partial or complete resolution of a RawRef
    """
    is_ambiguous = False

    def __init__(self, raw_ref: RawRef, resolved_parts: List[RawRefPart], node, ref: text.Ref, context_ref: text.Ref = None, context_type: ContextType = None) -> None:
        self.raw_ref = raw_ref
        self.resolved_parts = resolved_parts
        self.node = node
        self.ref = ref
        self.context_ref = context_ref
        self.context_type = context_type

    def clone(self, **kwargs) -> 'ResolvedRef':
        """
        Return new ResolvedRef with all the same data except modifications specified in kwargs
        """
        return ResolvedRef(**{**self.__dict__, **kwargs})

    def has_prev_unused_numbered_ref_part(self, part: RawRefPart) -> bool:
        """
        Helper function to avoid matching AddressInteger sections out of order
        Returns True if there is a RawRefPart which immediately precedes `raw_ref_part` and is not yet included in this match
        """
        prev_part = self.raw_ref.prev_num_parts_map.get(part, None)
        if prev_part is None: return False
        return prev_part not in set(self.resolved_parts)

    def has_prev_unused_numbered_ref_part_for_node(self, part: RawRefPart, lang: str, node: schema.SchemaNode) -> bool:
        """
        For SchemaNodes or ArrayMapNodes that have numeric equivalents (e.g. an alt struct for perek)
        make sure we are not matching AddressIntegers out of order. See self.has_prev_unused_numbered_ref_part()
        """
        if part.type != RefPartType.NUMBERED or \
                not getattr(node, 'numeric_equivalent', False) or \
                not self.has_prev_unused_numbered_ref_part(part):
            return False
        try:
            possible_sections, possible_to_sections, addr_classes = schema.AddressInteger(0).get_all_possible_sections_from_string(lang, part.text, strip_prefixes=True)
            for sec, toSec, addr_class in zip(possible_sections, possible_to_sections, addr_classes):
                if sec != node.numeric_equivalent: continue
                if addr_class == schema.AddressInteger: return True
        except KeyError:
            return False

    def _get_refined_matches_for_dh_part(self, raw_ref_part: RawRefPart, refined_parts: List[RawRefPart], node: schema.DiburHamatchilNodeSet):
        """
        Finds dibur hamatchil ref which best matches `raw_ref_part`
        Currently a very simplistic algorithm
        If there is a DH match, return the corresponding ResolvedRef
        """
        best_matches = node.best_fuzzy_matches(raw_ref_part)
        # TODO modify self with final dh
        return [self.clone(resolved_parts=refined_parts, node=max_node, ref=text.Ref(max_node.ref)) for _, max_node, _ in best_matches]

    def _get_refined_refs_for_numbered_part(self, raw_ref_part: RawRefPart, refined_parts: List[RawRefPart], node, lang, fromSections: List[RawRefPart]=None) -> List[
        'ResolvedRef']:
        if node is None: return []
        try:
            possible_sections, possible_to_sections, addr_classes = node.address_class(0).get_all_possible_sections_from_string(lang, raw_ref_part.text, fromSections, strip_prefixes=True)
        except (IndexError, TypeError, KeyError):
            return []
        refined_refs = []
        addr_classes_used = []
        for sec, toSec, addr_class in zip(possible_sections, possible_to_sections, addr_classes):
            if self.has_prev_unused_numbered_ref_part(raw_ref_part) and addr_class == schema.AddressInteger:
                """
                If raw_ref has NUMBERED parts [a, b]
                and part b matches before part a
                and part b gets matched as AddressInteger
                discard match because AddressInteger parts need to match in order
                """
                continue
            try:
                refined_ref = self.ref.subref(sec)
                if toSec != sec:
                    to_ref = self.ref.subref(toSec)
                    refined_ref = refined_ref.to(to_ref)
                refined_refs += [refined_ref]
                addr_classes_used += [addr_class]
            except (InputError, AssertionError, AttributeError):
                continue
        return [self.clone(resolved_parts=refined_parts, node=node, ref=refined_ref) for refined_ref in refined_refs]

    def _get_refined_refs_for_numbered_context_part(self, sec_context: SectionContext, refined_parts: List[RawRefPart], node) -> List[
        'ResolvedRef']:
        if node is None or not node.address_matches_section_context(0, sec_context):
            return []
        try:
            refined_ref = self.ref.subref(sec_context.address)
        except (IndexError, AssertionError):
            return []
        return [self.clone(resolved_parts=refined_parts, node=node, ref=refined_ref)]

    def _get_refined_matches_for_ranged_sections(self, sections: List['RawRefPart'], refined_parts: List[RawRefPart], node, lang, fromSections: list=None):
        resolved_raw_refs = [self.clone(resolved_parts=refined_parts, node=node)]
        incomplete_resolved_raw_refs = []
        is_first_pass = True
        for section_part in sections:
            queue_len = len(resolved_raw_refs)
            for _ in range(queue_len):
                temp_resolved_raw_ref = resolved_raw_refs.pop(0)
                if not is_first_pass:
                    temp_resolved_raw_ref.node = temp_resolved_raw_ref.node.get_referenceable_child(temp_resolved_raw_ref.ref)
                is_first_pass = False
                next_resolved_raw_refs = temp_resolved_raw_ref._get_refined_refs_for_numbered_part(section_part, refined_parts, temp_resolved_raw_ref.node, lang, fromSections)
                resolved_raw_refs += next_resolved_raw_refs
                if len(next_resolved_raw_refs) == 0 and False:
                    # disabling incomplete ranged ref matches to avoid false positives
                    incomplete_resolved_raw_refs += [temp_resolved_raw_ref]
        return resolved_raw_refs, incomplete_resolved_raw_refs

    def _get_refined_matches_for_ranged_part(self, raw_ref_part: RangedRawRefParts, refined_parts: List[RawRefPart], node, lang) -> List[
        'ResolvedRef']:
        section_resolved_raw_refs, incomplete_section_refs = self._get_refined_matches_for_ranged_sections(raw_ref_part.sections, refined_parts, node, lang)
        toSection_resolved_raw_refs, _ = self._get_refined_matches_for_ranged_sections(raw_ref_part.toSections, refined_parts, node, lang, fromSections=[x.ref.sections for x in section_resolved_raw_refs])
        ranged_resolved_raw_refs = []
        for section, toSection in product(section_resolved_raw_refs, toSection_resolved_raw_refs):
            try:
                ranged_resolved_raw_refs += [self.clone(resolved_parts=refined_parts, node=section.node, ref=section.ref.to(toSection.ref))]
            except InputError:
                continue
        if len(section_resolved_raw_refs) == 0:
            # TODO do we only want to include incomplete refs when they are no complete ones? probably.
            ranged_resolved_raw_refs += incomplete_section_refs
        return ranged_resolved_raw_refs

    def get_refined_matches(self, part: RawRefPart, node, lang: str) -> List['ResolvedRef']:
        refined_ref_parts = self.resolved_parts + [part]
        matches = []
        if isinstance(node, schema.TitledTreeNode) and node.is_default() and node.parent is not None:
            # default node automatically matches but doesnt append any new ref part to match
            matches += [self.clone(resolved_parts=self.resolved_parts, node=node, ref=node.ref())]
        # see NumberedTitledTreeNode.get_referenceable_child() for why we check if parent is None
        elif part.type == RefPartType.NUMBERED and isinstance(node, schema.JaggedArrayNode) and node.parent is None:
            if isinstance(part, SectionContext):
                matches += self._get_refined_refs_for_numbered_context_part(part, refined_ref_parts, node)
            else:
                matches += self._get_refined_refs_for_numbered_part(part, refined_ref_parts, node, lang)
        elif part.type == RefPartType.RANGE and isinstance(node, schema.JaggedArrayNode):
            matches += self._get_refined_matches_for_ranged_part(part, refined_ref_parts, node, lang)
        elif (part.type == RefPartType.NAMED and isinstance(node, schema.TitledTreeNode) or
              part.type == RefPartType.NUMBERED and isinstance(node, schema.ArrayMapNode)) or \
              part.type == RefPartType.NUMBERED and isinstance(node, schema.SchemaNode): # for case of numbered alt structs or schema nodes that look numbered (e.g. perakim and parshiot of Sifra)
            if node.ref_part_title_trie(lang).has_continuations(part.key(), key_is_id=part.key_is_id) and not self.has_prev_unused_numbered_ref_part_for_node(part, lang, node):
                matches += [self.clone(resolved_parts=refined_ref_parts, node=node, ref=node.ref())]
        elif part.type == RefPartType.DH:
            if isinstance(node, schema.JaggedArrayNode):
                # jagged array node can be skipped entirely if it has a dh child
                # technically doesn't work if there is a referenceable child in between ja and dh node
                node = node.get_referenceable_child(self.ref)
            if isinstance(node, schema.DiburHamatchilNodeSet):
                matches += self._get_refined_matches_for_dh_part(part, refined_ref_parts, node)
        # TODO sham and directional cases
        return matches
    
    def get_resolved_parts(self, include: Iterable[type] = None, exclude: Iterable[type] = None) -> List[RawRefPart]:
        """
        Returns list of resolved_parts according to criteria `include` and `exclude`
        If neither `include` nor `exclude` is passed, return all parts in `self.resolved_parts`
        :param include: if not None, only include parts that are an instance of at least one class specified in `include`
        :param exclude: if not None, exclude parts that are an instance of at least one class specified in `exclude`
        """
        parts = []
        for part in self.resolved_parts:
            if include is not None and not any(isinstance(part, typ) for typ in include):
                continue
            if exclude is not None and any(isinstance(part, typ) for typ in exclude):
                continue
            parts += [part]
        return parts

    def num_resolved(self, include: Iterable[type] = None, exclude: Iterable[type] = None) -> int:
        return len(self.get_resolved_parts(include, exclude))

    def get_node_children(self):
        """
        Get children of currently matched node to try to further refine match
        TODO can we make this less spaghetti code-ish?
        """
        if self.node is None:
            children = []
        elif isinstance(self.node, schema.NumberedTitledTreeNode):
            child = self.node.get_referenceable_child(self.ref)
            children = [] if child is None else [child]
        elif isinstance(self.node, schema.DiburHamatchilNode):
            children = []
        elif isinstance(self.node, text.Index):
            children = self.node.referenceable_children()
        else:
            children = self.node.children
        return children

    @property
    def order_key(self):
        """
        For sorting
        """
        return len(self.resolved_parts)


class AmbiguousResolvedRef:
    """
    Container for multiple ambiguous ResolvedRefs
    """
    is_ambiguous = True

    def __init__(self, resolved_refs: List[ResolvedRef]):
        if len(resolved_refs) == 0:
            raise InputError("Length of `resolved_refs` must be at least 1")
        self.resolved_raw_refs = resolved_refs
        self.raw_ref = resolved_refs[0].raw_ref  # assumption is all resolved_refs share same raw_ref. expose at top level


class MatchTemplateTrie:
    """
    Trie for titles. Keys are titles from match_templates on nodes.
    E.g. if there is match template with term slugs ["term1", "term2"], term1 has title "Term 1", term2 has title "Term 2"
    then an entry in the trie would be {"Term 1": {"Term 2": ...}}
    """
    def __init__(self, lang, nodes=None, sub_trie=None, scope=None) -> None:
        """
        :param lang:
        :param nodes:
        :param sub_trie:
        :param scope: str. scope of the trie. if 'alone', take into account `match_templates` marked with scope "alone" or "any".
        """
        self.lang = lang
        self.scope = scope
        if nodes is not None:
            self.__init_with_nodes(nodes)
        else:
            self._trie = sub_trie

    def __init_with_nodes(self, nodes):
        from .schema import TitledTreeNode
        self._trie = {}
        for node in nodes:
            assert isinstance(node, TitledTreeNode)
            is_index_level = getattr(node, 'index', False) and node == node.index.nodes
            for match_template in node.get_match_templates():
                if not is_index_level and self.scope != 'any' and match_template.scope != 'any' and self.scope != match_template.scope: continue
                curr_dict_queue = [self._trie]
                for term in match_template.terms:
                    len_curr_dict_queue = len(curr_dict_queue)
                    for _ in range(len_curr_dict_queue):
                        curr_dict = curr_dict_queue.pop(0)
                        curr_dict_queue += self.__get_sub_tries_for_term(term, curr_dict)
                # add nodes to leaves
                for curr_dict in curr_dict_queue:
                    leaf_node = node.index if is_index_level else node
                    if LEAF_TRIE_ENTRY in curr_dict:
                        curr_dict[LEAF_TRIE_ENTRY] += [leaf_node]
                    else:
                        curr_dict[LEAF_TRIE_ENTRY] = [leaf_node]

    @staticmethod
    def __get_sub_trie_for_new_key(key: str, curr_trie: dict) -> dict:
        if key in curr_trie:
            sub_trie = curr_trie[key]
        else:
            sub_trie = {}
            curr_trie[key] = sub_trie
        return sub_trie

    def __get_sub_tries_for_term(self, term: NonUniqueTerm, curr_trie: dict) -> List[dict]:
        sub_tries = []
        for title in term.get_titles(self.lang):
            sub_tries += [self.__get_sub_trie_for_new_key(title, curr_trie)]
        # also add term's key to trie for lookups from context ref parts
        sub_tries += [self.__get_sub_trie_for_new_key(TermContext(term).key(), curr_trie)]
        return sub_tries

    def __getitem__(self, key):
        return self.get(key)        

    def get(self, key, default=None):
        sub_trie = self._trie.get(key, default)
        if sub_trie is None: return
        return MatchTemplateTrie(self.lang, sub_trie=sub_trie, scope=self.scope)

    def has_continuations(self, key: str, key_is_id=False) -> bool:
        """
        Does trie have continuations for `key`?
        :param key: key to look up in trie. may need to be split into multiple keys to find a continuation.
        :param key_is_id: True if key is ID that cannot be split into smaller keys (e.g. slug).
        """
        return self.get_continuations(key, default=None, key_is_id=key_is_id) is not None

    @staticmethod
    def _merge_two_tries(a, b):
        "merges b into a"
        for key in b:
            if key in a:
                if isinstance(a[key], dict) and isinstance(b[key], dict):
                    MatchTemplateTrie._merge_two_tries(a[key], b[key])
                elif a[key] == b[key]:
                    pass  # same leaf value
                elif isinstance(a[key], list) and isinstance(b[key], list):
                    a[key] += b[key]
                else:
                    raise Exception('Conflict in _merge_two_tries')
            else:
                a[key] = b[key]
        return a

    @staticmethod
    def _merge_n_tries(*tries):
        if len(tries) == 1:
            return tries[0]
        return reduce(MatchTemplateTrie._merge_two_tries, tries)

    def get_continuations(self, key: str, default=None, key_is_id=False):
        continuations = self._get_continuations_recursive(key, key_is_id=key_is_id)
        if len(continuations) == 0:
            return default
        merged = self._merge_n_tries(*continuations)
        return MatchTemplateTrie(self.lang, sub_trie=merged, scope=self.scope)

    def _get_continuations_recursive(self, key: str, prev_sub_tries=None, key_is_id=False):
        from sefaria.utils.hebrew import get_prefixless_inds

        prev_sub_tries = prev_sub_tries or self._trie
        if key_is_id:
            # dont attempt to split key
            return [prev_sub_tries[key]] if key in prev_sub_tries else []
        next_sub_tries = []
        key = key.strip()
        starti_list = [0]
        if self.lang == 'he':
            starti_list += get_prefixless_inds(key)
        for starti in starti_list:
            for endi in reversed(range(len(key)+1)):
                sub_key = key[starti:endi]
                if sub_key not in prev_sub_tries: continue
                if endi == len(key):
                    next_sub_tries += [prev_sub_tries[sub_key]]
                    continue
                temp_sub_tries = self._get_continuations_recursive(key[endi:], prev_sub_tries[sub_key])
                next_sub_tries += temp_sub_tries
        return next_sub_tries

    def __contains__(self, key):
        return key in self._trie

    def __iter__(self):
        for item in self._trie:
            yield item


class MatchTemplateGraph:
    """
    DAG which represents connections between terms in index titles
    where each connection is a pair of consecutive terms
    """
    def __init__(self, nodes: List[schema.TitledTreeNode]):
        self._graph = defaultdict(set)
        for node in nodes:
            for match_template in node.get_match_templates():
                if len(match_template.term_slugs) < 2: continue
                terms = list(match_template.terms)
                for iterm, term in enumerate(terms[:-1]):
                    next_term = terms[iterm+1]
                    if term.ref_part_role == 'structural' and next_term.ref_part_role == 'structural':
                        self._graph[term.slug].add(next_term.slug)

    def parent_has_child(self, parent: str, child: str) -> bool:
        """
        For case where context is Yerushalmi Berakhot 1:1 and ref is Shabbat 1:1. Want to infer that we're referring to
        Yerushalmi Shabbat
        """
        return child in self._graph[parent]

    def do_parents_share_child(self, parent1: str, parent2: str, child: str) -> bool:
        """
        For case where context is Yerushalmi Berakhot 1:1 and ref is Bavli 2a. Want to infer that we're referring to
        Bavli Berakhot 2a b/c Yerushalmi and Bavli share child Berakhot
        """
        return self.parent_has_child(parent1, child) and self.parent_has_child(parent2, child)

    def get_parent_for_children(self, context_match_templates: List[MatchTemplate], input_slugs: list) -> Optional[str]:
        for template in context_match_templates:
            for context_slug in template.term_slugs:
                for input_slug in input_slugs:
                    if self.parent_has_child(context_slug, input_slug):
                        return context_slug

    def get_shared_child(self, context_match_templates: List[MatchTemplate], input_slugs: List[str]) -> Optional[str]:
        for template in context_match_templates:
            for i, context_slug in enumerate(template.term_slugs[:-1]):
                next_context_slug = template.term_slugs[i+1]
                for input_slug in input_slugs:
                    if self.do_parents_share_child(context_slug, input_slug, next_context_slug):
                        return next_context_slug


class TermMatcher:
    """
    Used to match raw ref parts to non-unique terms naively.
    Stores all existing terms for speed.
    Used in context matching.
    """
    def __init__(self, lang: str, nonunique_terms: NonUniqueTermSet) -> None:
        self.lang = lang
        self._str2term_map = defaultdict(list)
        for term in nonunique_terms:
            for title in term.get_titles(lang):
                self._str2term_map[title] += [term]

    def match_term(self, ref_part: RawRefPart) -> List[NonUniqueTerm]:
        from sefaria.utils.hebrew import get_prefixless_inds

        matches = []
        if ref_part.type != RefPartType.NAMED: return matches
        starti_inds = [0]
        if self.lang == 'he':
            starti_inds += get_prefixless_inds(ref_part.text)
        for starti in starti_inds:
            matches += self._str2term_map.get(ref_part.text[starti:], [])
        return matches

    def match_terms(self, ref_parts: List[RawRefPart]) -> List[NonUniqueTerm]:
        matches = []
        for part in ref_parts:
            matches += self.match_term(part)
        matches = list({m.slug: m for m in matches}.values())  # unique
        return matches


class IbidHistory:

    def __init__(self, last_n_to_store: int = 3):
        self.last_n_to_store = last_n_to_store
        self._last_match: Optional[text.Ref] = None
        self._last_titles: List[str] = []
        self._title_ref_map: Dict[str, text.Ref] = {}

    def _get_last_match(self) -> Optional[text.Ref]:
        return self._last_match

    def _set_last_match(self, oref: text.Ref):
        self._last_match = oref
        title = oref.index.title
        if title not in self._title_ref_map:
            self._last_titles += [title]
        self._title_ref_map[oref.index.title] = oref

        # enforce last_n_to_store
        if len(self._last_titles) > self.last_n_to_store:
            oldest_title = self._last_titles.pop(0)
            del self._title_ref_map[oldest_title]

    last_match = property(_get_last_match, _set_last_match)

    def get_match_by_title(self, title: str) -> Optional[text.Ref]:
        return self._title_ref_map.get(title, None)


class RefResolver:

    def __init__(self, raw_ref_model_by_lang: Dict[str, Language], raw_ref_part_model_by_lang: Dict[str, Language],
                 ref_part_title_trie_by_lang: Dict[str, MatchTemplateTrie], ref_part_title_graph: MatchTemplateGraph,
                 term_matcher_by_lang: Dict[str, TermMatcher]) -> None:
        self._raw_ref_model_by_lang = raw_ref_model_by_lang
        self._raw_ref_part_model_by_lang = raw_ref_part_model_by_lang
        self._ref_part_title_trie_by_lang = ref_part_title_trie_by_lang
        self._ref_part_title_graph = ref_part_title_graph
        self._term_matcher_by_lang = term_matcher_by_lang
        self._ibid_history = IbidHistory()

    def reset_ibid_history(self):
        self._ibid_history = IbidHistory()

    def bulk_resolve_refs(self, lang: str, book_context_refs: List[Optional[text.Ref]], input: List[str], with_failures=False, verbose=False, reset_ibids_every_context_ref=True) -> List[List[Union[ResolvedRef, AmbiguousResolvedRef]]]:
        self.reset_ibid_history()
        all_raw_refs = self._bulk_get_raw_refs(lang, input)
        resolved = []
        iter = zip(book_context_refs, all_raw_refs)
        if verbose:
            iter = tqdm(iter, total=len(book_context_refs))
        for book_context_ref, raw_refs in iter:
            if reset_ibids_every_context_ref:
                self.reset_ibid_history()
            inner_resolved = []
            for raw_ref in raw_refs:
                temp_resolved = self.resolve_raw_ref(lang, book_context_ref, raw_ref)
                if len(temp_resolved) == 0:
                    self.reset_ibid_history()
                    if with_failures:
                        inner_resolved += [ResolvedRef(raw_ref, [], None, None, context_ref=book_context_ref)]
                elif any(r.is_ambiguous for r in temp_resolved):
                    # can't be sure about future ibid inferences
                    # TODO can probably salvage parts of history if matches are ambiguous within one book
                    self.reset_ibid_history()
                else:
                    self._ibid_history.last_match = temp_resolved[-1].ref
                inner_resolved += temp_resolved
            resolved += [inner_resolved]
        return resolved

    def _bulk_get_raw_refs(self, lang: str, input: List[str]) -> List[List[RawRef]]:
        all_raw_ref_spans = list(self._bulk_get_raw_ref_spans(lang, input))
        ref_part_input = reduce(lambda a, b: a + [(sub_b.text, b[0]) for sub_b in b[1]], enumerate(all_raw_ref_spans), [])
        all_raw_ref_part_spans = list(self._bulk_get_raw_ref_part_spans(lang, ref_part_input, as_tuples=True))
        all_raw_ref_part_span_map = defaultdict(list)
        for ref_part_span, input_idx in all_raw_ref_part_spans:
            all_raw_ref_part_span_map[input_idx] += [ref_part_span]

        all_raw_refs = []
        for input_idx, raw_ref_spans in enumerate(all_raw_ref_spans):
            raw_ref_part_spans = all_raw_ref_part_span_map[input_idx]
            raw_refs = []
            for ispan, (span, part_span_list) in enumerate(zip(raw_ref_spans, raw_ref_part_spans)):
                raw_ref_parts = []
                for ipart, part_span in enumerate(part_span_list):
                    part_type = RefPartType.span_label_to_enum(part_span.label_)
                    dh_cont = None
                    if part_type == RefPartType.DH:
                        dh_cont = self._get_dh_continuation(ispan, ipart, raw_ref_spans, part_span_list, span, part_span)
                    raw_ref_parts += [RawRefPart(part_type, part_span, dh_cont)]
                raw_refs += [RawRef(raw_ref_parts, span)]
            all_raw_refs += [raw_refs]
        return all_raw_refs
    
    @staticmethod
    def _get_dh_continuation(ispan: int, ipart: int, raw_ref_spans: List[SpanOrToken], part_span_list: List[SpanOrToken], span: SpanOrToken, part_span: SpanOrToken) -> Optional[SpanOrToken]:
        if ipart == len(part_span_list) - 1:
            curr_doc = span.doc
            _, span_end = span_inds(span)
            if ispan == len(raw_ref_spans) - 1:
                dh_cont = curr_doc[span_end:]
            else:
                next_span_start, _ = span_inds(raw_ref_spans[ispan + 1])
                dh_cont = curr_doc[span_end:next_span_start]
        else:
            _, part_span_end = span_inds(part_span)
            next_part_span_start, _ = span_inds(part_span_list[ipart + 1])
            dh_cont = part_span.doc[part_span_end:next_part_span_start]

        return dh_cont

    def __get_attr_by_lang(self, lang: str, by_lang_attr: dict, error_msg: str):
        try:
            return by_lang_attr[lang]
        except KeyError as e:
            raise KeyError(f"{error_msg} for lang `{lang}`")

    def get_raw_ref_model(self, lang: str) -> Language:
        return self.__get_attr_by_lang(lang, self._raw_ref_model_by_lang, 'No Raw Ref Model')

    def get_raw_ref_part_model(self, lang: str) -> Language:
        return self.__get_attr_by_lang(lang, self._raw_ref_part_model_by_lang, 'No Raw Ref Model')

    def get_ref_part_title_trie(self, lang: str) -> MatchTemplateTrie:
        return self.__get_attr_by_lang(lang, self._ref_part_title_trie_by_lang, 'No Raw Ref Part Title Trie')

    def get_term_matcher(self, lang: str) -> TermMatcher:
        return self.__get_attr_by_lang(lang, self._term_matcher_by_lang, 'No Term Matcher')

    def _get_raw_ref_spans_in_string(self, lang: str, st: str) -> List[Span]:
        doc = self.get_raw_ref_model(lang)(st)
        return doc.ents

    def _bulk_get_raw_ref_spans(self, lang: str, input: List[str], batch_size=150, **kwargs) -> Generator[List[Span], None, None]:
        for doc in self.get_raw_ref_model(lang).pipe(input, batch_size=batch_size, **kwargs):
            if kwargs.get('as_tuples', False):
                doc, context = doc
                yield doc.ents, context
            else:
                yield doc.ents

    def _get_raw_ref_part_spans_in_string(self, lang: str, st: str) -> List[Span]:
        doc = self.get_raw_ref_part_model(lang)(st)
        return doc.ents

    def _bulk_get_raw_ref_part_spans(self, lang: str, input: List[str], batch_size=None, **kwargs) -> Generator[List[Span], None, None]:
        for doc in self.get_raw_ref_part_model(lang).pipe(input, batch_size=batch_size or len(input), **kwargs):
            if kwargs.get('as_tuples', False):
                doc, context = doc
                yield doc.ents, context
            else:
                yield doc.ents

    @staticmethod
    def split_non_cts_parts(raw_ref: RawRef) -> List[RawRef]:
        if not any(part.type == RefPartType.NON_CTS for part in raw_ref.raw_ref_parts): return [raw_ref]
        split_raw_refs = []
        curr_parts = []
        curr_part_start = 0
        for ipart, part in enumerate(raw_ref.raw_ref_parts):
            if part.type != RefPartType.NON_CTS:
                curr_parts += [part]
            if part.type == RefPartType.NON_CTS or ipart == len(raw_ref.raw_ref_parts) - 1:
                if len(curr_parts) == 0: continue
                curr_part_end = ipart  # exclude curr part which is NON_CTS
                if ipart == len(raw_ref.raw_ref_parts) - 1: curr_part_end = ipart + 1  # include curr part
                try:
                    split_raw_refs += [RawRef(curr_parts, raw_ref.subspan(slice(curr_part_start, curr_part_end)))]
                except AssertionError:
                    pass
                curr_parts = []
                curr_part_start = ipart+1
        return split_raw_refs

    def resolve_raw_ref(self, lang: str, book_context_ref: Optional[text.Ref], raw_ref: RawRef) -> List[Union[ResolvedRef, AmbiguousResolvedRef]]:
        split_raw_refs = self.split_non_cts_parts(raw_ref)
        resolved_list = []
        for i, temp_raw_ref in enumerate(split_raw_refs):
            is_non_cts = i > 0 and len(resolved_list) > 0
            if is_non_cts:
                # TODO assumes context is only first resolved ref
                book_context_ref = resolved_list[0].ref
            context_swap_map = None if book_context_ref is None else getattr(book_context_ref.index.nodes,
                                                                        'ref_resolver_context_swaps', None)
            self._apply_context_swaps(lang, raw_ref, context_swap_map)
            unrefined_matches = self.get_unrefined_ref_part_matches(lang, book_context_ref, temp_raw_ref)
            if is_non_cts:
                # filter unrefined matches to matches that resolved previously
                resolved_titles = {r.ref.index.title for r in resolved_list}
                unrefined_matches = list(filter(lambda x: x.ref.index.title in resolved_titles, unrefined_matches))
                # resolution will start at context_ref.sections - len(ref parts). rough heuristic
                for match in unrefined_matches:
                    try:
                        match.ref = match.ref.subref(book_context_ref.sections[:-len(temp_raw_ref.raw_ref_parts)])
                    except (InputError, AttributeError):
                        continue
            temp_resolved_list = self.refine_ref_part_matches(lang, book_context_ref, unrefined_matches, temp_raw_ref)
            if len(temp_resolved_list) > 1:
                resolved_list += [AmbiguousResolvedRef(temp_resolved_list)]
            else:
                resolved_list += temp_resolved_list
        return resolved_list

    def get_unrefined_ref_part_matches(self, lang: str, book_context_ref: Optional[text.Ref], raw_ref: RawRef) -> List[
        'ResolvedRef']:
        context_free_matches = self._get_unrefined_ref_part_matches_recursive(lang, raw_ref, ref_parts=raw_ref.parts_to_match)
        context_full_matches = []
        contexts = ((book_context_ref, ContextType.CURRENT_BOOK), (self._ibid_history.last_match, ContextType.IBID))
        for context_ref, context_type in contexts:
            context_full_matches += self._get_unrefined_ref_part_matches_for_graph_context(lang, context_ref, context_type, raw_ref)
        matches = context_full_matches + context_free_matches
        if len(matches) == 0:
            # TODO current assumption is only need to add context title if no matches. but it's possible this is necessary even if there were matches
            for context_ref, context_type in contexts:
                matches += self._get_unrefined_ref_part_matches_for_title_context(lang, context_ref, raw_ref, context_type)
        return matches

    def _get_unrefined_ref_part_matches_for_title_context(self, lang: str, context_ref: Optional[text.Ref], raw_ref: RawRef, context_type: ContextType) -> List[ResolvedRef]:
        matches = []
        if context_ref is None:
            return matches
        # assumption is longest template will be uniquest. is there a reason to consider other templates?
        longest_template = max(context_ref.index.nodes.get_match_templates(), key=lambda x: len(list(x.terms)))
        temp_ref_parts = raw_ref.parts_to_match + [TermContext(term) for term in longest_template.terms]
        temp_matches = self._get_unrefined_ref_part_matches_recursive(lang, raw_ref, ref_parts=temp_ref_parts)
        matches += list(filter(lambda x: x.num_resolved(include={TermContext}), temp_matches))
        for match in matches:
            match.context_ref = context_ref
            match.context_type = context_type
        return matches

    def _get_unrefined_ref_part_matches_for_graph_context(self, lang: str, context_ref: Optional[text.Ref], context_type: ContextType, raw_ref: RawRef) -> List[ResolvedRef]:
        matches = []
        if context_ref is None:
            return matches
        context_match_templates = list(context_ref.index.nodes.get_match_templates())
        raw_ref_term_slugs = [term.slug for term in self.get_term_matcher(lang).match_terms(raw_ref.raw_ref_parts)]
        context_parent = self._ref_part_title_graph.get_parent_for_children(context_match_templates, raw_ref_term_slugs)
        # I dont think this is necessary anymore since this case is covered by title context
        # context_child = self._ref_part_title_graph.get_shared_child(context_match_templates, raw_ref_term_slugs)
        for context_slug in (context_parent,):
            if context_slug is None: continue
            term_context = TermContext(NonUniqueTerm.init(context_slug))
            temp_matches = self._get_unrefined_ref_part_matches_recursive(lang, raw_ref, ref_parts=raw_ref.parts_to_match + [term_context])
            matches += list(filter(lambda x: x.num_resolved(exclude={TermContext}) and x.num_resolved(include={TermContext}), temp_matches))
            for match in matches:
                match.context_ref = context_ref
                match.context_type = context_type
        return matches

    def _apply_context_swaps(self, lang: str, raw_ref: RawRef, context_swap_map: Dict[str, str]=None):
        """
        Use `context_swap_map` to swap matching element of `ref_parts`
        Allows us to redefine how a ref part is interpreted depending on the context
        E.g. some rishonim refer to other rishonim based on nicknames

        Modifies `raw_ref` with updated ref_parts
        """
        swapped_ref_parts = []
        term_matcher = self.get_term_matcher(lang)
        if context_swap_map is None: return
        for part in raw_ref.raw_ref_parts:
            # TODO assumes only one match in term_matches
            term_matches = term_matcher.match_term(part)
            found_match = False
            for match in term_matches:
                if match.slug not in context_swap_map: continue
                swapped_ref_parts += [TermContext(NonUniqueTerm.init(slug)) for slug in context_swap_map[match.slug]]
                found_match = True
                break
            if not found_match: swapped_ref_parts += [part]
        raw_ref.parts_to_match = swapped_ref_parts

    def _get_unrefined_ref_part_matches_recursive(self, lang: str, raw_ref: RawRef, title_trie: MatchTemplateTrie = None, ref_parts: list = None, prev_ref_parts: list = None) -> List[ResolvedRef]:
        title_trie = title_trie or self.get_ref_part_title_trie(lang)
        prev_ref_parts = prev_ref_parts or []
        matches = []
        for part in ref_parts:
            # no need to consider other types at root level
            if part.type != RefPartType.NAMED: continue

            temp_prev_ref_parts = prev_ref_parts + [part]
            temp_title_trie = title_trie.get_continuations(part.key())
            if temp_title_trie is None: continue
            if LEAF_TRIE_ENTRY in temp_title_trie:
                matches += [ResolvedRef(raw_ref, temp_prev_ref_parts, node, (node.nodes if isinstance(node, text.Index) else node).ref()) for node in temp_title_trie[LEAF_TRIE_ENTRY]]
            temp_ref_parts = [temp_part for temp_part in ref_parts if temp_part != part]
            matches += self._get_unrefined_ref_part_matches_recursive(lang, raw_ref, temp_title_trie, ref_parts=temp_ref_parts, prev_ref_parts=temp_prev_ref_parts)

        return self._prune_unrefined_ref_part_matches(matches)

    def refine_ref_part_matches(self, lang: str, book_context_ref: Optional[text.Ref], ref_part_matches: List[ResolvedRef], raw_ref: RawRef) -> List[ResolvedRef]:
        matches = []
        for unrefined_match in ref_part_matches:
            unused_parts = list(set(raw_ref.parts_to_match) - set(unrefined_match.resolved_parts))
            matches += self._get_refined_ref_part_matches_recursive(lang, unrefined_match, unused_parts)

            # context
            # if unrefined_match already used context, make sure it continues to use it
            # otherwise, consider other possible context
            context_ref_list = [book_context_ref, self._ibid_history.last_match] if unrefined_match.context_ref is None else [unrefined_match.context_ref]
            context_type_list = [ContextType.CURRENT_BOOK, ContextType.IBID] if unrefined_match.context_ref is None else [unrefined_match.context_type]
            for context_ref, context_type in zip(context_ref_list, context_type_list):
                matches += self._get_refined_ref_part_matches_for_section_context(lang, context_ref, context_type, unrefined_match, unused_parts)
        return self._prune_refined_ref_part_matches(matches)

    @staticmethod
    def _get_section_contexts(context_ref: text.Ref, match_index: text.Index, common_index: text.Index) -> List[SectionContext]:
        """
        Currently doesn't work if any of the indexes are complex texts
        Returns list section contexts extracted from `context_node`
        :param context_ref: context ref where we are searching
        :param match_index: Index of current match we are trying to refine
        :param common_index: Index
        """
        def get_section_set(index: text.Index) -> Set[Tuple[str, str, bool]]:
            root_node = index.nodes.get_default_child() or index.nodes
            try:
                referenceable_sections = getattr(root_node, 'referenceableSections', [True] * len(root_node.addressTypes))
                return set(zip(root_node.addressTypes, root_node.sectionNames, referenceable_sections))
            except AttributeError:
                # complex text
                return set()
        context_node = context_ref.index_node
        referenceable_sections = getattr(context_node, 'referenceableSections', [True]*len(context_node.addressTypes))
        context_sec_list = list(zip(context_node.addressTypes, context_node.sectionNames, referenceable_sections))
        match_sec_set  = get_section_set(match_index)
        common_sec_set = get_section_set(common_index) & match_sec_set & set(context_sec_list)
        if len(common_sec_set) == 0: return []
        sec_contexts = []
        for isec, sec_tuple in enumerate(context_sec_list):
            if sec_tuple in common_sec_set and isec < len(context_ref.sections):
                addr_type_str, sec_name, referenceable = sec_tuple
                if not referenceable: continue
                addr_type = schema.AddressType.to_class_by_address_type(addr_type_str)
                sec_contexts += [SectionContext(addr_type, sec_name, context_ref.sections[isec])]
        return sec_contexts

    @staticmethod
    def _get_refined_ref_part_matches_for_section_context(lang: str, context_ref: Optional[text.Ref], context_type: ContextType, ref_part_match: ResolvedRef, ref_parts: List[RawRefPart]) -> List[ResolvedRef]:
        """
        Tries to infer sections from context ref and uses them to refine `ref_part_match`
        """
        if context_ref is None: return []
        context_titles = set(getattr(context_ref.index, 'base_text_titles', [])) | {context_ref.index.title}
        match_titles = set(getattr(ref_part_match.ref.index, 'base_text_titles', [])) | {ref_part_match.ref.index.title}
        matches = []
        for common_base_text in (context_titles & match_titles):
            common_index = text.library.get_index(common_base_text)
            sec_contexts = RefResolver._get_section_contexts(context_ref, ref_part_match.ref.index, common_index)
            matches += RefResolver._get_refined_ref_part_matches_recursive(lang, ref_part_match, ref_parts + sec_contexts)
        # remove matches which dont use context
        matches = list(filter(lambda x: x.num_resolved(include={SectionContext}), matches))
        for match in matches:
            match.context_ref = context_ref
            match.context_type = context_type
        return matches

    @staticmethod
    def _get_refined_ref_part_matches_recursive(lang: str, match: ResolvedRef, ref_parts: List[RawRefPart]) -> List[ResolvedRef]:
        fully_refined = []
        children = match.get_node_children()
        for part in ref_parts:
            for child in children:
                temp_matches = match.get_refined_matches(part, child, lang)
                for temp_match in temp_matches:
                    temp_ref_parts = list(set(ref_parts) - set(temp_match.resolved_parts))
                    fully_refined += RefResolver._get_refined_ref_part_matches_recursive(lang, temp_match, temp_ref_parts)
        if len(fully_refined) == 0:
            # original match is better than no matches
            return [match]
        return fully_refined

    @staticmethod
    def _prune_unrefined_ref_part_matches(ref_part_matches: List[ResolvedRef]) -> List[ResolvedRef]:
        index_match_map = defaultdict(list)
        for match in ref_part_matches:
            key = match.node.title if isinstance(match.node, text.Index) else match.node.ref().normal()
            index_match_map[key] += [match]
        pruned_matches = []
        for match_list in index_match_map.values():
            pruned_matches += [max(match_list, key=lambda m: m.num_resolved())]
        return pruned_matches

    @staticmethod
    def _prune_refined_ref_part_matches(resolved_refs: List[ResolvedRef]) -> List[ResolvedRef]:
        """
        Applies some heuristics to remove false positives
        """

        # remove matches that have empty refs
        # TODO removing for now b/c of yerushalmi project. doesn't seem necessary to happen here anyway.
        # resolved_refs = list(filter(lambda x: not x.ref.is_empty(), resolved_refs))

        # remove matches that don't match all ref parts to avoid false positives
        # used to only apply to context matches
        def filter_context_matches(match: ResolvedRef) -> bool:
            if match.num_resolved(include={ContextPart}) == 0:
                # no context
                # return True
                pass

            # make sure no explicit sections matched before context sections
            first_explicit_section = None
            for part in match.get_resolved_parts():
                if not first_explicit_section and part.type == RefPartType.NUMBERED and not part.is_context:
                    first_explicit_section = part
                elif first_explicit_section and part.is_context:
                    return False

            resolved_explicit = set(match.get_resolved_parts(exclude={ContextPart}))
            to_match_explicit = {part for part in match.raw_ref.parts_to_match if not part.is_context}

            if match.context_type in CONTEXT_TO_REF_PART_TYPE.keys():
                # remove an equivalent number of context parts that were resolved from to_match_explicit to approximate
                # comparison. this is a bit hacky but seems to work for all known cases so far.
                num_parts_to_remove = match.num_resolved(include={ContextPart})
                for _ in range(num_parts_to_remove):
                    part = next((p for p in to_match_explicit if p.type in CONTEXT_TO_REF_PART_TYPE[match.context_type]), None)
                    if part is None:
                        break  # no more
                    to_match_explicit.remove(part)
            return resolved_explicit == to_match_explicit

        resolved_refs = list(filter(filter_context_matches, resolved_refs))
        if len(resolved_refs) == 0: return resolved_refs

        # if any context-free match uses all input parts, dont need to try context
        context_free_matches = list(filter(lambda m: m.context_ref is None and set(m.get_resolved_parts()) == set(m.raw_ref.parts_to_match), resolved_refs))
        if len(context_free_matches) > 0:
            resolved_refs = context_free_matches

        resolved_refs.sort(key=lambda x: x.order_key, reverse=True)
        top_order_key = resolved_refs[0].order_key
        max_resolved_refs = []
        for resolved_ref in resolved_refs:
            if resolved_ref.order_key != top_order_key: break
            max_resolved_refs += [resolved_ref]

        # make unique
        max_resolved_refs = list({r.ref: r for r in max_resolved_refs}.values())

        return max_resolved_refs