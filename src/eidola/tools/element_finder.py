"""Smart Element Finder for XML-based UI element discovery.

Provides fast, deterministic element finding based on XML hierarchy,
with fallback chains and AI-friendly element formatting.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .selectors import (
    INSTAGRAM_SELECTORS,
    SelectorConfig,
    get_all_selectors,
)

if TYPE_CHECKING:
    pass


@dataclass
class FoundElement:
    """Represents a found UI element."""
    
    selector_name: str | None
    """Name of the selector that matched (if from registry)."""
    
    text: str
    """Text content of the element."""
    
    resource_id: str
    """resource-id attribute."""
    
    content_desc: str
    """content-desc attribute (accessibility description)."""
    
    class_name: str
    """Android class name."""
    
    package: str
    """Package name."""
    
    bounds: tuple[int, int, int, int]
    """Bounding box: (x1, y1, x2, y2)."""
    
    center: tuple[int, int]
    """Center point: (x, y)."""
    
    clickable: bool
    """Whether element is clickable."""
    
    focusable: bool
    """Whether element is focusable."""
    
    enabled: bool
    """Whether element is enabled."""
    
    checkable: bool = False
    """Whether element is checkable (checkbox, radio)."""
    
    checked: bool = False
    """Whether element is checked."""
    
    scrollable: bool = False
    """Whether element is scrollable."""
    
    @property
    def display_text(self) -> str:
        """Get the best text to display for this element."""
        return self.text or self.content_desc or self.resource_id.split("/")[-1] if self.resource_id else ""
    
    @property
    def width(self) -> int:
        """Element width in pixels."""
        return self.bounds[2] - self.bounds[0]
    
    @property
    def height(self) -> int:
        """Element height in pixels."""
        return self.bounds[3] - self.bounds[1]


# Mapping from selector keys to XML attributes
SELECTOR_TO_XML = {
    "text": "text",
    "textContains": "text",
    "resourceId": "resource-id",
    "description": "content-desc",
    "descriptionContains": "content-desc",
    "className": "class",
    "clickable": "clickable",
    "focusable": "focusable",
    "enabled": "enabled",
    "instance": None,  # Special handling
}


class SmartElementFinder:
    """Fast XML-based element finder with caching and fallbacks."""
    
    def __init__(self, xml_str: str):
        """Initialize finder with XML hierarchy.
        
        Args:
            xml_str: XML dump of the screen hierarchy
        """
        self._xml = xml_str
        self._root = ET.fromstring(xml_str)
        self._cache: dict[str, FoundElement | None] = {}
        self._all_elements: list[FoundElement] | None = None
    
    def find(self, selector_name: str) -> FoundElement | None:
        """Find element by selector name from registry.
        
        Uses primary selector first, then fallbacks if not found.
        Results are cached for repeated lookups.
        
        Args:
            selector_name: Name from INSTAGRAM_SELECTORS
            
        Returns:
            FoundElement if found, None otherwise
        """
        # Check cache
        if selector_name in self._cache:
            return self._cache[selector_name]
        
        # Get all selectors (primary + fallbacks)
        selectors = get_all_selectors(selector_name)
        if not selectors:
            self._cache[selector_name] = None
            return None
        
        # Try each selector in order
        for selector in selectors:
            element = self._find_by_selector(selector)
            if element:
                element.selector_name = selector_name
                self._cache[selector_name] = element
                return element
        
        self._cache[selector_name] = None
        return None
    
    def find_by_text(self, text: str, partial: bool = True) -> FoundElement | None:
        """Find element by text content.
        
        Args:
            text: Text to search for
            partial: If True, match partial text (contains)
            
        Returns:
            FoundElement if found, None otherwise
        """
        text_lower = text.lower()
        
        for node in self._root.iter("node"):
            node_text = node.get("text", "").lower()
            node_desc = node.get("content-desc", "").lower()
            
            if partial:
                if text_lower in node_text or text_lower in node_desc:
                    return self._node_to_element(node)
            else:
                if text_lower == node_text or text_lower == node_desc:
                    return self._node_to_element(node)
        
        return None
    
    def find_by_resource_id(self, resource_id: str, partial: bool = True) -> FoundElement | None:
        """Find element by resource-id.
        
        Args:
            resource_id: Resource ID to search for
            partial: If True, match partial resource-id (contains)
            
        Returns:
            FoundElement if found, None otherwise
        """
        search = resource_id.lower()
        
        for node in self._root.iter("node"):
            node_rid = node.get("resource-id", "").lower()
            
            if partial:
                if search in node_rid:
                    return self._node_to_element(node)
            else:
                if search == node_rid:
                    return self._node_to_element(node)
        
        return None
    
    def find_by_content_desc(self, desc: str, partial: bool = True) -> FoundElement | None:
        """Find element by content-desc (accessibility description).
        
        Args:
            desc: Content description to search for
            partial: If True, match partial description (contains)
            
        Returns:
            FoundElement if found, None otherwise
        """
        desc_lower = desc.lower()
        
        for node in self._root.iter("node"):
            node_desc = node.get("content-desc", "").lower()
            
            if partial:
                if desc_lower in node_desc:
                    return self._node_to_element(node)
            else:
                if desc_lower == node_desc:
                    return self._node_to_element(node)
        
        return None
    
    def find_by_bounds(self, bounds: tuple[int, int, int, int], tolerance: int = 10) -> FoundElement | None:
        """Find element by approximate bounds.
        
        Args:
            bounds: Target bounds (x1, y1, x2, y2)
            tolerance: Pixel tolerance for matching
            
        Returns:
            FoundElement if found within tolerance, None otherwise
        """
        x1, y1, x2, y2 = bounds
        
        for node in self._root.iter("node"):
            node_bounds = self._parse_bounds(node.get("bounds", ""))
            if not node_bounds:
                continue
            
            nx1, ny1, nx2, ny2 = node_bounds
            
            if (
                abs(nx1 - x1) <= tolerance and
                abs(ny1 - y1) <= tolerance and
                abs(nx2 - x2) <= tolerance and
                abs(ny2 - y2) <= tolerance
            ):
                return self._node_to_element(node)
        
        return None
    
    def exists(self, selector_name: str) -> bool:
        """Check if element exists (for quick verification).
        
        Args:
            selector_name: Name from INSTAGRAM_SELECTORS
            
        Returns:
            True if element exists
        """
        return self.find(selector_name) is not None
    
    def get_all_elements(self) -> list[FoundElement]:
        """Get all elements in the hierarchy.
        
        Returns:
            List of all FoundElement objects
        """
        if self._all_elements is not None:
            return self._all_elements
        
        elements = []
        for node in self._root.iter("node"):
            element = self._node_to_element(node)
            if element:
                elements.append(element)
        
        self._all_elements = elements
        return elements
    
    def get_elements_for_ai(self, max_elements: int = 40) -> list[dict[str, Any]]:
        """Get UI elements formatted for AI decision making.
        
        Filters and formats elements to be useful for AI navigation decisions.
        Prioritizes elements with text, content-desc, or clickable state.
        
        Args:
            max_elements: Maximum number of elements to return
            
        Returns:
            List of dicts with element info for AI prompts
        """
        results = []
        
        for node in self._root.iter("node"):
            text = node.get("text", "")
            desc = node.get("content-desc", "")
            res_id = node.get("resource-id", "")
            clickable = node.get("clickable") == "true"
            enabled = node.get("enabled", "true") == "true"
            
            # Skip elements without useful info
            if not text and not desc:
                continue
            
            # Skip disabled elements
            if not enabled:
                continue
            
            # Parse bounds
            bounds = self._parse_bounds(node.get("bounds", ""))
            center_x, center_y = 0, 0
            if bounds:
                center_x = (bounds[0] + bounds[2]) // 2
                center_y = (bounds[1] + bounds[3]) // 2
            
            elem = {
                "display": text or desc,
                "text": text,
                "content_desc": desc,
                "clickable": clickable,
                "bounds": {"x": center_x, "y": center_y},
            }
            
            # Add short resource_id only if Instagram-specific
            if res_id and "instagram" in res_id.lower():
                short_id = res_id.split("/")[-1] if "/" in res_id else res_id
                elem["resource_id"] = short_id
            
            results.append(elem)
            
            if len(results) >= max_elements:
                break
        
        return results
    
    def get_clickable_elements(self) -> list[FoundElement]:
        """Get all clickable elements.
        
        Returns:
            List of clickable FoundElement objects
        """
        elements = []
        for node in self._root.iter("node"):
            if node.get("clickable") == "true":
                element = self._node_to_element(node)
                if element:
                    elements.append(element)
        return elements
    
    def get_scrollable_element(self) -> FoundElement | None:
        """Get the main scrollable element (for scroll operations).
        
        Returns:
            Scrollable FoundElement if found, None otherwise
        """
        for node in self._root.iter("node"):
            if node.get("scrollable") == "true":
                return self._node_to_element(node)
        return None
    
    def _find_by_selector(self, selector: SelectorConfig) -> FoundElement | None:
        """Find element matching a single selector config.
        
        Args:
            selector: Selector configuration dict
            
        Returns:
            FoundElement if found, None otherwise
        """
        instance_target = selector.get("instance", 0)
        instance_count = 0
        
        for node in self._root.iter("node"):
            if self._matches_selector(node, selector):
                if instance_count == instance_target:
                    return self._node_to_element(node)
                instance_count += 1
        
        return None
    
    def _matches_selector(self, node: ET.Element, selector: SelectorConfig) -> bool:
        """Check if XML node matches all selector criteria.
        
        Args:
            node: XML element node
            selector: Selector configuration dict
            
        Returns:
            True if node matches all criteria
        """
        for key, value in selector.items():
            if key == "instance":
                continue  # Handled separately
            
            xml_attr = SELECTOR_TO_XML.get(key, key)
            if xml_attr is None:
                continue
            
            node_value = node.get(xml_attr, "")
            
            # Handle "Contains" suffix - partial match
            if key.endswith("Contains"):
                if value.lower() not in node_value.lower():
                    return False
            # Handle boolean values
            elif isinstance(value, bool):
                node_bool = node_value.lower() == "true"
                if node_bool != value:
                    return False
            # Exact match
            else:
                if node_value != value:
                    return False
        
        return True
    
    def _node_to_element(self, node: ET.Element) -> FoundElement | None:
        """Convert XML node to FoundElement.
        
        Args:
            node: XML element node
            
        Returns:
            FoundElement or None if bounds can't be parsed
        """
        bounds = self._parse_bounds(node.get("bounds", ""))
        if not bounds:
            return None
        
        center = (
            (bounds[0] + bounds[2]) // 2,
            (bounds[1] + bounds[3]) // 2,
        )
        
        return FoundElement(
            selector_name=None,
            text=node.get("text", ""),
            resource_id=node.get("resource-id", ""),
            content_desc=node.get("content-desc", ""),
            class_name=node.get("class", ""),
            package=node.get("package", ""),
            bounds=bounds,
            center=center,
            clickable=node.get("clickable") == "true",
            focusable=node.get("focusable") == "true",
            enabled=node.get("enabled", "true") == "true",
            checkable=node.get("checkable") == "true",
            checked=node.get("checked") == "true",
            scrollable=node.get("scrollable") == "true",
        )
    
    def _parse_bounds(self, bounds_str: str) -> tuple[int, int, int, int] | None:
        """Parse bounds string to tuple.
        
        Args:
            bounds_str: Bounds in format "[x1,y1][x2,y2]"
            
        Returns:
            Tuple (x1, y1, x2, y2) or None if invalid
        """
        if not bounds_str:
            return None
        
        match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
        if not match:
            return None
        
        return tuple(int(x) for x in match.groups())  # type: ignore


def create_finder(xml_str: str) -> SmartElementFinder:
    """Create a SmartElementFinder from XML string.
    
    Args:
        xml_str: XML dump of the screen hierarchy
        
    Returns:
        Configured SmartElementFinder instance
    """
    return SmartElementFinder(xml_str)
