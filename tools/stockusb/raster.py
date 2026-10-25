"""MG2500 plain-paper BJ raster encoder, matching canon_bjl.c/bjl_render.c.

One image, A4/Letter, 300/600 dpi. Raster coordinates are the printable area;
the protocol supplies the same physical borders as the ESP32 implementation.
"""
from dataclasses import dataclass
import struct
import numpy as np

PREFIX='<?xml version="1.0" encoding="utf-8" ?><cmd xmlns:ivec="http://www.canon.com/ns/cmd/2008/07/common/"'
START=(PREFIX+'><ivec:contents><ivec:operation>StartJob</ivec:operation>'
       '<ivec:param_set servicetype="print"><ivec:jobID>00000001</ivec:jobID>'
       '<ivec:bidi>0</ivec:bidi></ivec:param_set></ivec:contents></cmd>').encode()
MODE=(PREFIX+' xmlns:vcn="http://www.canon.com/ns/cmd/2008/07/canon/">'
      '<ivec:contents><ivec:operation>VendorCmd</ivec:operation>'
      '<ivec:param_set servicetype="print"><vcn:ijoperation>ModeShift</vcn:ijoperation>'
      '<vcn:ijmode>1</vcn:ijmode><ivec:jobID>00000001</ivec:jobID>'
      '</ivec:param_set></ivec:contents></cmd>').encode()
END=b'\x1b@'+(PREFIX+'><ivec:contents><ivec:operation>EndJob</ivec:operation>'
    '<ivec:param_set servicetype="print"><ivec:jobID>00000001</ivec:jobID>'
    '</ivec:param_set></ivec:contents></cmd>').encode()
PAPERS={'A4':(595,842,3,4961,7016),'Letter':(612,792,13,5100,6600)}
BAYER=np.array([[0,32,8,40,2,34,10,42],[48,16,56,24,50,18,58,26],
 [12,44,4,36,14,46,6,38],[60,28,52,20,62,30,54,22],
 [3,35,11,43,1,33,9,41],[51,19,59,27,49,17,57,25],
 [15,47,7,39,13,45,5,37],[63,31,55,23,61,29,53,21]],dtype=np.uint16)


def esc(code,payload): return b'\x1b('+code.encode()+struct.pack('<H',len(payload))+payload


def packbits(data):
    """Use the C encoder's exact literal/repeat selection, including tails."""
    data=bytes(data);out=bytearray();i=0
    while i<len(data):
        lit=i
        while i+2<len(data) and not data[i]==data[i+1]==data[i+2]: i+=1
        while lit<i:
            n=min(128,i-lit);out.append(n-1);out.extend(data[lit:lit+n]);lit+=n
        v=data[i];start=i
        while i<len(data) and data[i]==v: i+=1
        count=i-start
        while count:
            n=min(128,count);out.extend(((1-n)&255,v));count-=n
    return bytes(out)


@dataclass(frozen=True)
class Page:
    dpi: int = 300
    paper: str = 'A4'

    def __post_init__(self):
        if self.dpi not in (300,600) or self.paper not in PAPERS: raise ValueError('Unsupported print mode')

    @property
    def width(self): return (PAPERS[self.paper][0]-20)*self.dpi//72
    @property
    def height(self): return (PAPERS[self.paper][1]-24)*self.dpi//72
    @property
    def bits(self): return (1,1,1,1) if self.dpi==300 else (2,2,1,1)

    def header(self):
        wpt,hpt,size,w600,h600=PAPERS[self.paper];aw,ah=wpt-20,hpt-24
        ink=bytearray(42);ink[:3]=b'\x80\x00\x01' if self.dpi==300 else b'\x80\x80\x01'
        for c,bits in enumerate(self.bits): ink[3+c*3]=bits;ink[5+c*3]=bits+1
        geom=bytearray(46)
        struct.pack_into('>H',geom,0,(ah+1)*5//6);struct.pack_into('>H',geom,4,(aw+1)*5//6)
        struct.pack_into('>H4I',geom,12,600,10*600//72,9*600//72,aw*600//72,ah*600//72)
        struct.pack_into('>II',geom,38,w600,h600)
        commands=[('b',b'\x01'),('q',b'\x01'),('d',struct.pack('>HH',self.dpi,self.dpi)),
                  ('t',ink),('c',bytes((0x30,0,1 if self.dpi==300 else 2))),('p',geom),
                  ('P',bytes((0,size,0,0,1,0,1,0,0))),('T',b'\x03\x04\x04'),
                  ('l',b'\x34\0\0'),('v',b'\0'),('I',b'\x01'),('J',b'\x08'),('L',b'CMYK')]
        return b'\x1b[K\x02\0\0\x0f'+b''.join(esc(k,v) for k,v in commands)


def ink_planes(rgb,bits,y):
    rgb=np.asarray(rgb,dtype=np.uint8)
    if rgb.ndim!=2 or rgb.shape[1]!=3: raise ValueError('Expected an RGB row')
    if np.all(rgb==255): return (b'',)*4
    cmy=255-rgb.astype(np.uint16);k=cmy.min(axis=1)
    denominator=np.maximum(255-k,1)
    densities=[((cmy[:,c]-k)*255//denominator).astype(np.uint8) for c in range(3)]+[k.astype(np.uint8)]
    v=np.arange(256,dtype=np.float32)/np.float32(255)
    # float32 and separate operations match the C tables (-ffp-contract=off).
    tables=[(np.float32(255)*v*(np.float32(1)-np.float32(g)*(np.float32(1)-v))+np.float32(.5)).astype(np.uint8)
            for g in (.25,.25,.25,.15)]
    threshold=np.resize(BAYER[y&7]*4+2,len(rgb))
    out=[]
    for c,b in enumerate(bits):
        density=tables[c][densities[c]]
        if not density.any(): out.append(b'');continue
        if b==1:
            packed=np.packbits(density>threshold,bitorder='big').tobytes()
        else:
            s=density.astype(np.uint16)*2
            levels=s//255
            levels+=((levels<2)&(s-levels*255>threshold)).astype(np.uint16)
            levels=np.pad(levels,(0,(-len(levels))%4))
            packed=np.sum(levels.reshape(-1,4)<<np.array([6,4,2,0]),axis=1).astype(np.uint8).tobytes()
        out.append(packbits(packed) if any(packed) else b'')
    return tuple(out)


def page_stream(page,rows):
    """Yield commands for one full page, padding a short row iterator white."""
    yield page.header()
    rows=iter(rows)
    block=[bytearray() for _ in range(4)]
    for y in range(page.height):
        row=next(rows,None)
        if row is None: planes=(b'',)*4
        else:
            if np.shape(row)!=(page.width,3): raise ValueError('Print row width does not match page')
            planes=ink_planes(row,page.bits,y)
        for c in range(4): block[c].extend(planes[c]+b'\x80')
        if y%8==7 or y==page.height-1:
            padding=(7-y%8) if y==page.height-1 else 0
            for c in range(4):
                yield esc('F',block[c]+b'\x80'*padding)
                block[c].clear()
    if next(rows,None) is not None: raise ValueError('Too many print rows')
    yield b'\x0c'+esc('b',b'\0')


def job_stream(page,rows):
    yield START;yield MODE;yield from page_stream(page,rows);yield END


def stamp_label(canvas,page,label):
    """Print a unique identification in the top margin so sheets stay apart."""
    from PIL import ImageDraw,ImageFont
    size=max(10,int(page.dpi*0.1))   # about 2.5 mm high
    try: font=ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc',size)
    except Exception: font=ImageFont.load_default()
    d=ImageDraw.Draw(canvas)
    d.rectangle((0,0,page.width-1,size+6),fill='white')
    d.text((int(page.dpi*0.1),2),label,fill='black',font=font)
    return canvas


def prepare_image(page,path=None,label=None):
    from PIL import Image,ImageOps,ImageDraw
    canvas=Image.new('RGB',(page.width,page.height),'white')
    if path:
        with Image.open(path) as original:
            rgba=ImageOps.exif_transpose(original).convert('RGBA')
            rgba=ImageOps.contain(rgba,canvas.size,method=Image.Resampling.LANCZOS)
            canvas.paste(rgba,((page.width-rgba.width)//2,(page.height-rgba.height)//2),rgba)
    else:
        # Deliberately small new test pattern, not the ESP32's full test page.
        d=ImageDraw.Draw(canvas);s=page.dpi/300
        d.text((int(50*s),int(50*s)),f'MG2550s Python / {page.dpi} dpi / {page.paper}',fill='black')
        for i,color in enumerate(('cyan','magenta','yellow','black','#808080')):
            x=int((50+i*150)*s);y=int(100*s)
            d.rectangle((x,y,x+int(100*s),y+int(100*s)),fill=color)
        d.line((int(50*s),int(230*s),int(800*s),int(230*s)),fill='black',width=max(1,int(s)))
    if label: stamp_label(canvas,page,label)
    return canvas
