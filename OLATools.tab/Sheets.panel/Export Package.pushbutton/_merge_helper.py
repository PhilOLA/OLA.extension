import sys
from pypdf import PdfWriter
output_path = sys.argv[1]
input_paths = sys.argv[2].split('|')
writer = PdfWriter()
for p in input_paths:
    writer.append(p)
with open(output_path, 'wb') as f:
    writer.write(f)
print('OK')
