import urllib.request
import xml.etree.ElementTree as ET

def convert_xspf_to_m3u(xspf_url, output_path):
    """
    Downloads an XSPF playlist and saves it as an M3U file.
    """
    print(f"Starting download from: {xspf_url}")
    
    try:
        # 1. Download the file, pretending to be a normal web browser
        req = urllib.request.Request(xspf_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            xml_data = response.read()
            
        # 2. Parse the XML
        root = ET.fromstring(xml_data)
        namespace = {'xspf': 'http://xspf.org/ns/0/'}
        
        # 3. Create the M3U file and start writing
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
            
            # Find every track/channel in the playlist
            track_count = 0
            for track in root.findall('.//xspf:track', namespace):
                
                # Get channel name
                title_elem = track.find('xspf:title', namespace)
                title = title_elem.text if title_elem is not None else "Unknown Channel"
                
                # Get stream URL
                loc_elem = track.find('xspf:location', namespace)
                location = loc_elem.text if loc_elem is not None else ""
                
                # Get logo (if it exists)
                image_elem = track.find('xspf:image', namespace)
                logo_url = image_elem.text if image_elem is not None else ""
                
                # Write to file if a stream URL exists
                if location:
                    if logo_url:
                        f.write(f'#EXTINF:-1 tvg-logo="{logo_url}",{title}\n')
                    else:
                        f.write(f"#EXTINF:-1,{title}\n")
                        
                    f.write(f"{location}\n")
                    track_count += 1
                    
        print(f"Success! Converted {track_count} channels and saved to: {output_path}")
        return True

    except Exception as e:
        print(f"Error converting playlist: {e}")
        return False

# --- LOCAL TESTING BLOCK ---
# This block ONLY runs if you execute this specific file directly on your computer.
if __name__ == "__main__":
    # We will use the Init7 URL to test if the engine works
    test_url = "https://api.init7.net/tvchannels.xspf"
    # It will save the file in the exact same folder you run the script from
    test_output = "test_channels.m3u"
    
    convert_xspf_to_m3u(test_url, test_output)