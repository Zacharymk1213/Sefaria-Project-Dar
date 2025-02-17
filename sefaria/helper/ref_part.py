from sefaria.model.ref_part import ResolvedRef, AmbiguousResolvedRef, TermContext, RefPartType
from sefaria.model import text
from typing import List, Union, Optional
from collections import defaultdict


def make_html(bulk_resolved_list: List[List[List[Union[ResolvedRef, AmbiguousResolvedRef]]]], texts: List[List[str]], output_filename, lang='he'):
    from sefaria.utils.util import wrap_chars_with_overlaps

    def get_resolved_metadata(resolved: ResolvedRef, i: int) -> dict:
        metadata =  {
            "i": i,
            "ref": resolved.ref,
            "orig_part_strs": [p.text for p in resolved.raw_ref.raw_ref_parts],
            "orig_part_types": [p.type.name for p in resolved.raw_ref.raw_ref_parts],
            "final_part_strs": [p.text for p in resolved.raw_ref.parts_to_match],
            "final_part_types": [p.type.name for p in resolved.raw_ref.parts_to_match],
            "resolved_part_strs": [p.term.slug if isinstance(p, TermContext) else p.text for p in resolved.resolved_parts],
            "resolved_part_types": [p.type.name for p in resolved.resolved_parts],
            "resolved_part_classes": [p.__class__.__name__ for p in resolved.resolved_parts],
            "context_ref": resolved.context_ref.normal() if resolved.context_ref else "N/A",
            "context_type": resolved.context_type.name if resolved.context_type else "N/A",
        }
        if RefPartType.RANGE.name in metadata['final_part_types']:
            range_part = next((p for p in resolved.raw_ref.parts_to_match if p.type == RefPartType.RANGE), None)
            metadata['input_range_sections'] = [p.text for p in range_part.sections]
            metadata['input_range_to_sections'] = [p.text for p in range_part.toSections]
        # dont think this is necessary if we already have input range sections
        # if RefPartType.RANGE.name in metadata['resolved_part_types']:
        #     range_part = next((p for p in resolved.resolved_parts if p.type == RefPartType.RANGE), None)
        #     metadata['resolved_range_sections'] = [p.text for p in range_part.sections]
        #     metadata['resolved_range_to_sections'] = [p.text for p in range_part.toSections]
        return metadata

    def get_inspect_html(metadata: dict, mention: str) -> str:
        show_final_ref_parts = metadata['orig_part_strs'] != metadata['final_part_strs']
        inspect_window = f'''
        <span id="inspect-window-{metadata['i']}" class="hidden inspect-window">
            <b>Input:</b>
            {mention}
            </br>
            <b>Resolved Ref:</b>
            {metadata['ref'].normal() if metadata['ref'] else 'N/A'}
            </br>
            <b>Context Ref</b>
            <table>
                <tr><td>Ref</td><td>{metadata['context_ref']}</td></tr>
                <tr><td>Type</td><td>{metadata['context_type']}</td></tr>          
            </table>
            <b>Input Ref Parts</b>
            <table>
                <tr><td>Text</td><td>{'</td><td>'.join(metadata['orig_part_strs'])}</td></tr>
                <tr><td>Type</td><td>{'</td><td>'.join(metadata['orig_part_types'])}</td></tr>
            </table>
            {f"""<b>Final Ref Parts</b>
            <table>
                <tr><td>Text</td><td>{'</td><td>'.join(metadata['final_part_strs'])}</td></tr>
                <tr><td>Type</td><td>{'</td><td>'.join(metadata['final_part_types'])}</td></tr>
            </table>""" if show_final_ref_parts else ''}
            {f"""<b>Input Range Sections: </b>{' | '.join(metadata['input_range_sections'])}</br>""" if metadata.get('input_range_sections', False) else ''}
            {f"""<b>Input Range To Sections: </b>{' | '.join(metadata['input_range_to_sections'])}</br>""" if metadata.get('input_range_to_sections', False) else ''}
            <b>Resolved Parts</b>
            <table>
                <tr><td>Text</td><td>{'</td><td>'.join(metadata['resolved_part_strs'])}</td></tr>
                <tr><td>Type</td><td>{'</td><td>'.join(metadata['resolved_part_types'])}</td></tr>
                <tr><td>Class</td><td>{'</td><td>'.join(metadata['resolved_part_classes'])}</td></tr>
            </table>
            {f"""<b>Resolved Range Sections: </b>{' | '.join(metadata['resolved_range_sections'])}</br>""" if metadata.get('resolved_range_sections', False) else ''}
            {f"""<b>Resolved Range To Sections: </b>{' | '.join(metadata['resolved_range_to_sections'])}</br>""" if metadata.get('resolved_range_to_sections', False) else ''}
            <button onclick="toggleWindow({metadata['i']})">Close</button>
        </span>
        '''
        return f"""
        <button class="inspect-btn" onclick="onInspectClick({metadata['i']})">Inspect</button>
        {inspect_window}
        """

    def get_wrapped_text(mention, metadata):
        start = f'<span class="{metadata["true condition"]} tag">'
        end = f'''
        <span class="label">{metadata["label"]}</span>
        {" ".join([get_inspect_html(inner_metadata, mention) for inner_metadata in metadata['data']])}
        </span>
        '''
        d = metadata['data']
        if len(d) == 1 and d[0]['ref'] is not None:
            ref = d[0]['ref']
            start += f'<a href="https://www.sefaria.org/{ref.url()}" target="_blank">'
            end = '</a>' + end
        return f'{start}{mention}{end}', len(start), len(end)

    html = """
    <html>
      <head>
        <style>
        body { max-width: 800px; margin-right: auto; margin-left: auto; }
        .doc { line-height: 200%; border-bottom: 2px black solid; padding-bottom: 20px; }
        .tag { padding: 5px; }
        .tp { background-color: greenyellow; border: 5px lightgreen solid; }
        .ambig { background-color: orange; border: 5px red solid; }
        .fp { background-color: pink; border: 5px lightgreen solid; }
        .fn { background-color: greenyellow; border: 5px pink solid; }
        .label { font-weight: bold; font-size: 75%; color: #666; padding-right: 5px; }
        .inspect-btn { margin: 0 5px; }
        .hidden { display: none; }
        td { border: 1px solid black; padding: 5px }
        table { margin-bottom: 20px; }
        .inspect-window {
            position: fixed;
            direction: ltr;
            top: 10px;
            right: 10px;
            background-color: #eee;
            border: 3px solid black;
            padding: 10px;
            overflow-y: auto;
            height: calc(100% - 40px);
        }
        </style>
        <script>
          let currIOpen = null;
          function onInspectClick(i) {
            toggleWindow(currIOpen);
            currIOpen = i;
            toggleWindow(i);
          }
          function toggleWindow(i) {
           const curr_window = document.getElementById("inspect-window-" + i);
           if (!curr_window) {
            console.log("Couldn't find window for ID", i);
            return;
            }
            if (curr_window.classList.contains("hidden")) {
                currIOpen = i;
                curr_window.classList.remove("hidden");
            } else {
                currIOpen = null;
                curr_window.classList.add("hidden");
            }
          }
        </script>
      </head>
      <body>
    """
    iwrapped = 0
    for bulk_resolved, inner_texts in zip(bulk_resolved_list, texts):
        for temp_resolved_list, input_text in zip(bulk_resolved, inner_texts):
            chars_to_wrap = []
            for resolved in temp_resolved_list:
                rlist = resolved.resolved_raw_refs if resolved.is_ambiguous else [resolved]
                start_char, end_char = rlist[0].raw_ref.char_indices
                metadata = {
                    "label": "מקור",
                    "true condition": "ambig" if resolved.is_ambiguous else "tp",
                    "data": [],
                }
                for r in rlist:
                    metadata['data'] += [get_resolved_metadata(r, iwrapped)]
                    iwrapped += 1
                chars_to_wrap += [(start_char, end_char, metadata)]
            wrapped_text = wrap_chars_with_overlaps(input_text, chars_to_wrap, get_wrapped_text)
            html += f"""
            <p dir="{'rtl' if lang == 'he' else 'ltr'}" class="doc">{wrapped_text}</p>
            """
    html += """
      </body>
    </html>
    """
    with open(output_filename, "w") as fout:
        fout.write(html)


def make_find_refs_response(resolved: List[List[Union[AmbiguousResolvedRef, ResolvedRef]]], with_text=False):
    ref_results = []
    ref_data = {}
    resolved_ref_list = [resolved_ref for inner_resolved in resolved for resolved_ref in inner_resolved]
    for resolved_ref in resolved_ref_list:
        resolved_refs = resolved_ref.resolved_raw_refs if resolved_ref.is_ambiguous else [resolved_ref]
        start_char, end_char = resolved_ref.raw_ref.char_indices
        text = resolved_ref.raw_ref.text
        link_failed = resolved_refs[0].ref is None
        ref_results += [{
            "startChar": start_char,
            "endChar": end_char,
            "text": text,
            "linkFailed": link_failed,
            "refs": None if link_failed else [rr.ref.normal() for rr in resolved_refs]
        }]
        for rr in resolved_refs:
            if rr.ref is None: continue
            tref = rr.ref.normal()
            if tref in ref_data: continue
            ref_data[tref] = make_ref_response_for_linker(rr.ref, with_text)

    response = {
        "results": ref_results,
        "refData": ref_data
    }
    return response


def make_ref_response_for_linker(oref: text.Ref, with_text=False) -> dict:
    res = {
        'heRef': oref.he_normal(),
        'url': oref.url(),
        'primaryCategory': oref.primary_category,
    }
    if with_text:
        text_fam = text.TextFamily(oref, commentary=0, context=0, pad=False, stripItags=True)
        he = text_fam.he
        en = text_fam.text
        res.update({
            'he': he,
            'en': en,
        })
    return res
