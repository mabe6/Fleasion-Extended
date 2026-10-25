"""Small English fallback for reusable Fleasion widgets in CacheSwap."""
import re

_LABELS = {
    "text": "‹", "text_2": "›", "v": "›",
    "search": "Search", "search_keys_and_values": "Search keys and values…",
    "search_instances_and_properties": "Search instances and properties…",
    "previous_match": "Previous match", "next_match": "Next match",
    "expand_all": "Expand all", "collapse_all": "Collapse all",
    "copy_value": "Copy value", "property": "Property", "value": "Value",
    "type": "Type", "add_child_to_selected_item_or_add": "Add child / item",
    "add_property_to_selected_item": "Add property", "invalid_classname": "Unknown Roblox class",
    "value_is_not_a_known_roblox_classname": "{value0} is not in the known class list.",
    "font_information": "Font information", "b_family_b_value": "Font family: {value0}", "font_could_not_be_loaded_for_preview": "The font could not be loaded for preview.",
    "modified": "Modified in preview", "search_classname": "Search class names…",
    "choose_classname": "Choose a class", "search_instances_and_properties": "Search instances and properties…",
}

def tr(key, **kwargs):
    text = _LABELS.get(str(key).rsplit('.', 1)[-1])
    if text is None:
        text = str(key).rsplit('.', 1)[-1].replace('_', ' ').capitalize()
    for k, value in kwargs.items():
        text = text.replace('{' + k + '}', str(value))
    return text

def tr_count(key, count, **kwargs):
    return tr(key, count=count, **kwargs)

def verbatim(value):
    return str(value)
