from http.server import BaseHTTPRequestHandler, HTTPServer











class TOPFRAServer(BaseHTTPRequestHandler):



    def do_GET(self):



        self.send_response(200)



        self.send_header("Content-type", "application/json")



        self.end_headers()



        response_message = '{"status": "success", "protocol": "Httpsh", "core": "TOPFRA", "message": "Local server connected successfully!"}'



        self.wfile.write(response_message.encode("utf-8"))







    def do_POST(self):



        content_length = int(self.headers.get('Content-Length', 0))



        post_data = self.rfile.read(content_length)



        self.send_response(200)



        self.send_header("Content-type", "application/json")



        self.end_headers()



        response_message = '{"status": "received", "core": "TOPFRA", "payload_status": "Pure Text Processed"}'



        self.wfile.write(response_message.encode("utf-8"))











if __name__ == "__main__":



    server_address = ('127.0.0.1', 8000)



    httpd = HTTPServer(server_address, TOPFRAServer)



    print("--- TOPFRA Httpsh Local Server Running on http://127.0.0.1:8000 ---")



    httpd.serve_forever() 