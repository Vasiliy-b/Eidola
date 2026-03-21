#!/usr/bin/env python3
"""Quick screen dump utility using FIRERPA SDK."""

import argparse
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def main():
    parser = argparse.ArgumentParser(description="Dump device screen XML")
    parser.add_argument("--device", "-d", required=True, help="Device IP")
    parser.add_argument("--output", "-o", default="dump.xml", help="Output file")
    parser.add_argument("--screenshot", "-s", action="store_true", help="Also take screenshot")
    args = parser.parse_args()
    
    try:
        from firerpa import Device
        
        print(f"Connecting to {args.device}...")
        device = Device(args.device)
        
        # Get XML dump
        print("Dumping UI hierarchy...")
        xml_content = device.dump_xml()
        
        if not xml_content or len(xml_content) < 100:
            print("WARNING: XML dump seems empty or too short!")
            print(f"Content length: {len(xml_content) if xml_content else 0}")
        
        # Save XML
        output_path = Path(args.output)
        output_path.write_text(xml_content, encoding="utf-8")
        print(f"Saved XML to: {output_path} ({len(xml_content)} bytes)")
        
        # Optional screenshot
        if args.screenshot:
            screenshot_path = output_path.with_suffix(".png")
            print("Taking screenshot...")
            screenshot_data = device.screenshot()
            screenshot_path.write_bytes(screenshot_data)
            print(f"Saved screenshot to: {screenshot_path}")
        
        # Quick summary
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(xml_content)
            node_count = len(list(root.iter("node")))
            print(f"XML contains {node_count} nodes")
            
            # Find Instagram elements
            ig_elements = []
            for node in root.iter("node"):
                res_id = node.get("resource-id", "")
                if "instagram" in res_id.lower():
                    text = node.get("text", "")[:30]
                    ig_elements.append(f"  {res_id.split('/')[-1]}: {text}")
            
            if ig_elements:
                print(f"\nInstagram elements ({len(ig_elements)}):")
                for el in ig_elements[:20]:
                    print(el)
                if len(ig_elements) > 20:
                    print(f"  ... and {len(ig_elements) - 20} more")
        except ET.ParseError as e:
            print(f"XML parse error: {e}")
        
    except ImportError:
        print("ERROR: firerpa not installed. Install with: pip install firerpa")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
