from typing import Tuple, Dict
import re

G = 1073741824
M = 1048576
K = 1024

keys = [64 * K, M, 16 * M, 64 * M, 256 * M, G, 2 * G, 4 * G]
key_names = ['64K', '1M', '16M', '64M', '256M', '1G', '2G', '4G']


def parse(fn) -> Tuple[Dict[int, float], Dict[int, float], float]:
    """Parses output of nccl test, returns dictionary of values suitable for logging in Gbit/second."""
    regex = re.compile('.*Avg bus bandwidth.+?:.([0-9.]+).*')
    alg_bw = {}
    bus_bw = {}
    avg_bw = -1
    output_started = False
    for line in open(fn):
        line = line.strip()
        if not line:
            continue
        toks = line.split()

        # wait for the first line of the form
        # "#       size         count    type   redop     time   algbw   busbw  error     time   algbw   busbw  error"
        if not output_started and len(line.split()) != 13:
            continue
        if 'size' in toks and 'time' in toks and 'busbw' in toks:
            output_started = True
            continue

        if not output_started:
            continue
        
        if regex.match(line):
            avg_bw = float(regex.findall(line)[0])*8

        if line.startswith('#') or len(toks) != 12:
            continue

        size = int(toks[0])
        if size in keys:
            alg_bw[size] = float(toks[9])*8
            bus_bw[size] = float(toks[10])*8

    return alg_bw, bus_bw, avg_bw


def make_readable(d, prefix: str) -> Dict[str, float]:
    """Translates size keys into easier to read labels, ie 1073741824 to 1G"""

    key_name_map = dict(zip(key_names, keys))

    readable_map = {}
    for name in key_names:
        if key_name_map[name] in d:
            readable_map[prefix + name] = d[key_name_map[name]]
    return readable_map


def main():
    print(parse('nccltest_output.txt'))
    alg_bw, bus_bw, bw = parse('nccltest_output.txt')
    print(make_readable(alg_bw, 'algbw_'))


if __name__ == '__main__':
    main()
