import json

class IperfServer:
    def __init__(self, host, port, options):
        self.host = host
        self.port = port
        self.options = options
    
    def __str__(self):
        return self.host

def parse_servers(filename):
    with open(filename, "r") as file:
        data = json.load(file)

    servers = []
    for entry in data:
        servers.append(IperfServer(
            entry["IP/HOST"],
            entry["PORT"],
            entry["OPTIONS"]
        ))

    return servers
