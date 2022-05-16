# -*- coding: utf-8 -*-

import django

django.setup()

import re
from sefaria.model import *
import csv


# The goal of this script is to identify broken links in the connections between
# the mishnah and talmud refs as found in the German text.
def clean_text(german_text):
    german_text = str(german_text)
    german_text = TextChunk._strip_itags(german_text)
    text_array = re.sub(r"\[|\]|\{|\}|<small>|<\/small>", "", german_text)
    return text_array

# This function generates a CSV given a list of dicts
def generate_csv(dict_list, headers, file_name):

    with open(f'mishnah_map_validation/{file_name}.csv', 'w') as file:
        c = csv.DictWriter(file, fieldnames=headers)
        c.writeheader()
        c.writerows(dict_list)

    print(f"File writing of {file_name} complete")


def get_ref_from_link(mishnah_talmud_link):
    mishnah_ref, talmud_ref = mishnah_talmud_link.refs if "Mishnah" in mishnah_talmud_link.refs[0] else reversed(
        mishnah_talmud_link.refs)
    return Ref(mishnah_ref), Ref(talmud_ref)


def remove_quotes(text):
    res = re.sub(r"(<i>.*?<\/i>)", '', text)
    return res


def get_german_text(talmud_ref):
    german_text = talmud_ref.text('en', vtitle='Talmud Bavli. German trans. by Lazarus Goldschmidt, 1929 [de]')
    german_text = german_text.text
    german_text = clean_text(german_text)
    german_text = remove_quotes(german_text)
    return german_text


def check_uppercase_percentage(talmud_ref):
    german_text = get_german_text(talmud_ref)
    if german_text:
        percent_uppercase = (sum(1 for c in german_text if c.isupper()) / len(german_text)) * 100
    else:
        percent_uppercase = -1  # Not applicable
    return percent_uppercase, german_text


def generate_data_append_to_list(data_list, talmud_ref, mishnah_ref, checking):
    percent_uppercase, german_text = check_uppercase_percentage(talmud_ref)
    mishnah_tref = "N.A" if isinstance(mishnah_ref, str) else mishnah_ref.normal()
    cur_link_data = {'mishnah_tref': mishnah_tref,
                     'talmud_tref': talmud_ref.normal(),
                     'german_text': german_text}

    if checking=='false-positive':
        if 50 >= percent_uppercase > 0:
            cur_link_data['issue'] = 'False positive'
            data_list.append(cur_link_data)

    elif checking == 'false-negative':
        if percent_uppercase >= 50 and len(cur_link_data['german_text']) > 50:
            cur_link_data['issue'] = 'False negative'
            data_list.append(cur_link_data)

    return cur_link_data


def get_list_link_talmud_segments():
    ls = LinkSet({"type": "mishnah in talmud"})
    linkset_list_mishnah_in_talmud_segments = []
    for link in ls:
        mishnah_ref, talmud_ref = get_ref_from_link(link)
        if talmud_ref.is_range():
            for ref in talmud_ref.range_list():
                linkset_list_mishnah_in_talmud_segments.append(ref.normal())
        else:
            linkset_list_mishnah_in_talmud_segments.append(talmud_ref.normal())
    return set(linkset_list_mishnah_in_talmud_segments)


# Phase One: Report on all lowercase heavy Talmud pieces
def phase_one():
    ls = LinkSet({"type": "mishnah in talmud"})
    data_list = []
    for link in ls:
        mishnah_ref, talmud_ref = get_ref_from_link(link)
        if talmud_ref.is_range():
            for ref in talmud_ref.range_list():
                generate_data_append_to_list(data_list, ref, mishnah_ref, checking='false-positive')
        else:
            generate_data_append_to_list(data_list, talmud_ref, mishnah_ref, checking='false-positive')

    # CSV
    # TODO - condense w filter?
    csv_list = []
    for each in data_list:
        if 'issue' in each:
            csv_list.append(each)

    return csv_list


    # Phase Two - Cross check
# - Next step for mapping
#     -  Passage collection
#     - Not really used in prod at all
#     - Sugya less relevant, Mishnah more relevant
#         -
# - Validate collection
#     - Check each Mishnah’s segment
#     - If each talmud ref is majority capital
#     - Then a good db to use
# - Then use this (filtered for mishnahs) and use it as base
def phase_two(csv_list):
    print("inside p2")

    linkset_segments = get_list_link_talmud_segments()

    print('got ls')
    # action - check if tref is in mishnah map. If it is, ignore.
    # Else, check if text of segment is maj. all caps. If it is, flag as false negative.
    def action(segment_str, tref, he_tref, version):
        if tref not in linkset_segments:
            generate_data_append_to_list(csv_list, Ref(tref), 'replace with mishnah ref', checking='false-negative')

    bavli_indices = library.get_indexes_in_category("Bavli", full_records=True)

    print('got indices, getting version')
    count = 0
    for index in bavli_indices:
        german_talmud = Version().load(
            {"title": index.title, "versionTitle": 'Talmud Bavli. German trans. by Lazarus Goldschmidt, 1929 [de]'})
        if german_talmud:
            german_talmud.walk_thru_contents(action)
            count +=1
            print(f'walking thru contents {count} times')


    return csv_list



if __name__ == "__main__":
    csv_list = phase_one()
    print("p1 complete")
    csv_list = phase_two(csv_list)
    print("p2 complete")
    csv_list.sort(key=lambda x: Ref(x["talmud_tref"]).order_id())
    generate_csv(csv_list, ['mishnah_tref', 'talmud_tref', 'german_text', 'issue'], 'main_issues')
