import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

q = "residual reinforcement learning quadrotor"
prefix = "all"
words = q.split()

# 1. Using space + AND + space and letting it encode to %20
q_str1 = " AND ".join([f"{prefix}:{w}" for w in words])
enc1 = urllib.parse.quote(q_str1, safe=':*')

# 2. Using literal +AND+
q_str2 = "+AND+".join([f"{prefix}:{w}" for w in words])
enc2 = urllib.parse.quote(q_str2, safe='+:*')

def test_q(name, enc):
    url = f"http://export.arxiv.org/api/query?search_query={enc}&max_results=2"
    req = urllib.request.Request(url)
    resp = urllib.request.urlopen(req).read()
    root = ET.fromstring(resp)
    ns = {'opensearch': 'http://a9.com/-/spec/opensearch/1.1/'}
    total = root.find('opensearch:totalResults', ns).text
    print(f"[{name}] Encoded: {enc} => Total: {total}")

test_q("Space AND Space", enc1)
test_q("Literal +AND+", enc2)
