"""Turn Appium page source XML (iOS XCUITest or Android UiAutomator2) into Elements."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .base import Element, Rect

_IOS_TYPES = {
    "XCUIElementTypeButton": "button",
    "XCUIElementTypeStaticText": "text",
    "XCUIElementTypeTextField": "input",
    "XCUIElementTypeSecureTextField": "input",
    "XCUIElementTypeTextView": "input",
    "XCUIElementTypeSearchField": "input",
    "XCUIElementTypeImage": "image",
    "XCUIElementTypeSwitch": "switch",
    "XCUIElementTypeSlider": "slider",
    "XCUIElementTypeTable": "list",
    "XCUIElementTypeCollectionView": "list",
    "XCUIElementTypeScrollView": "list",
    "XCUIElementTypeCell": "cell",
    "XCUIElementTypeLink": "link",
    "XCUIElementTypeTabBar": "tabbar",
    "XCUIElementTypeNavigationBar": "navbar",
    "XCUIElementTypeAlert": "alert",
    "XCUIElementTypeSheet": "alert",
    "XCUIElementTypePicker": "picker",
    "XCUIElementTypePickerWheel": "picker",
    "XCUIElementTypeSegmentedControl": "tab",
    "XCUIElementTypeStepper": "stepper",
    "XCUIElementTypeWebView": "webview",
    "XCUIElementTypeMap": "map",
    "XCUIElementTypeKeyboard": "keyboard",
    "XCUIElementTypeCheckBox": "checkbox",
    "XCUIElementTypeToggle": "switch",
}

_ANDROID_TYPES = [
    ("Button", "button"), ("ImageButton", "button"), ("FloatingActionButton", "button"),
    ("EditText", "input"), ("AutoCompleteTextView", "input"), ("SearchView", "input"),
    ("Switch", "switch"), ("ToggleButton", "switch"), ("SwitchCompat", "switch"),
    ("CheckBox", "checkbox"), ("RadioButton", "checkbox"), ("CheckedTextView", "checkbox"),
    ("SeekBar", "slider"), ("RatingBar", "slider"),
    ("RecyclerView", "list"), ("ListView", "list"), ("ScrollView", "list"), ("ViewPager", "list"),
    ("GridView", "list"), ("HorizontalScrollView", "list"), ("NestedScrollView", "list"),
    ("TabLayout", "tabbar"), ("BottomNavigationView", "tabbar"), ("TabWidget", "tabbar"),
    ("Toolbar", "navbar"), ("ActionBar", "navbar"),
    ("WebView", "webview"), ("MapView", "map"),
    ("Spinner", "picker"), ("NumberPicker", "picker"), ("DatePicker", "picker"), ("TimePicker", "picker"),
    ("ImageView", "image"), ("TextView", "text"), ("ProgressBar", "progress"),
]


def _android_type(cls: str) -> str:
    short = cls.split(".")[-1]
    for key, t in _ANDROID_TYPES:
        if short.endswith(key):
            return t
    return "other"


def _bool(v: str | None) -> bool:
    return str(v).lower() == "true"


_bounds_re = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def parse_source(xml: str, platform: str) -> list[Element]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        # Appium sometimes emits invalid chars; strip and retry
        xml = re.sub(r"[^\x09\x0A\x0D\x20-퟿-�]", "", xml)
        root = ET.fromstring(xml)
    out: list[Element] = []
    _walk(root, platform, "", "", 0, out)
    return out


def _walk(node: ET.Element, platform: str, idpath: str, xpath: str, depth: int, out: list[Element]) -> None:
    kids = list(node)
    # xpath by position among siblings of the same tag — resolvable on both platforms
    if platform == "ios":
        el = _ios_element(node, idpath or "0", xpath or f"/{node.tag}", depth, len(kids))
    else:
        el = _android_element(node, idpath or "0", xpath or f"/{node.tag}", depth, len(kids))
    if el is not None:
        out.append(el)
    tag_counts: dict[str, int] = {}
    for i, k in enumerate(kids):
        tag_counts[k.tag] = tag_counts.get(k.tag, 0) + 1
        child_xpath = f"{xpath or '/' + node.tag}/{k.tag}[{tag_counts[k.tag]}]"
        _walk(k, platform, f"{idpath or '0'}.{i}", child_xpath, depth + 1, out)


def _ios_element(n: ET.Element, idpath: str, xpath: str, depth: int, nkids: int) -> Element | None:
    a = n.attrib
    t = n.tag
    if t in ("AppiumAUT",):
        return None
    try:
        rect = Rect(int(float(a.get("x", 0))), int(float(a.get("y", 0))),
                    int(float(a.get("width", 0))), int(float(a.get("height", 0))))
    except ValueError:
        rect = Rect(0, 0, 0, 0)
    typ = _IOS_TYPES.get(t, "other")
    if t == "XCUIElementTypeOther" and a.get("name") and nkids == 0:
        typ = "button" if _bool(a.get("accessible")) else "other"
    value = a.get("value") or ""
    checked = None
    if typ in ("switch", "checkbox"):
        checked = value in ("1", "true", "on")
    return Element(
        id=idpath, type=typ, raw_type=t,
        text=(a.get("label") or value or "").strip() if typ != "input" else value.strip(),
        label=(a.get("label") or "").strip(),
        resource_id=(a.get("name") or "").strip(),
        rect=rect,
        clickable=typ in ("button", "cell", "link", "tab", "switch", "checkbox", "stepper"),
        enabled=_bool(a.get("enabled", "true")),
        visible=_bool(a.get("visible", "true")),
        focused=_bool(a.get("focused")),
        checked=checked,
        scrollable=typ == "list",
        editable=typ == "input",
        password=t == "XCUIElementTypeSecureTextField",
        depth=depth, children=nkids, xpath=xpath,
    )


def _android_element(n: ET.Element, idpath: str, xpath: str, depth: int, nkids: int) -> Element | None:
    a = n.attrib
    cls = a.get("class") or n.tag
    if n.tag == "hierarchy":
        return None
    m = _bounds_re.match(a.get("bounds", ""))
    if m:
        x1, y1, x2, y2 = map(int, m.groups())
        rect = Rect(x1, y1, x2 - x1, y2 - y1)
    else:
        rect = Rect(0, 0, 0, 0)
    typ = _android_type(cls)
    if typ in ("other", "text", "image") and _bool(a.get("clickable")):
        typ = "button" if typ != "text" else "button"
    checkable = _bool(a.get("checkable"))
    rid = a.get("resource-id", "")
    rid_short = rid.split("/")[-1] if rid else ""
    return Element(
        id=idpath, type=typ, raw_type=cls,
        text=(a.get("text") or "").strip(),
        label=(a.get("content-desc") or "").strip(),
        resource_id=rid_short,
        rect=rect,
        clickable=_bool(a.get("clickable")) or _bool(a.get("long-clickable")),
        enabled=_bool(a.get("enabled", "true")),
        visible=_bool(a.get("displayed", "true")),
        focused=_bool(a.get("focused")),
        checked=_bool(a.get("checked")) if checkable else None,
        scrollable=_bool(a.get("scrollable")),
        editable=typ == "input" or _bool(a.get("editable")),
        password=_bool(a.get("password")),
        depth=depth, children=nkids, xpath=xpath,
    )
