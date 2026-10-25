/* Test adapter only. Build against an explicitly supplied, unmodified
 * hobby-printer-esp32 checkout; none of its code is a Python runtime dependency.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fake_scanner.h"
#include "canon_bjl.h"
#include "bjl_render.h"

void *fake_new(void) {
    fake_scanner_t *f=calloc(1,sizeof(*f));
    if (!f) return NULL;
    if (fake_scanner_init(f,0)) {free(f);return NULL;}
    return f;
}
void fake_delete(void *p) {fake_scanner_free(p);free(p);}
int fake_write(void *p,const unsigned char *b,size_t n,int timeout) {
    pixma_io_t io=fake_scanner_io(p);return io.write(io.ctx,b,n,timeout);
}
int fake_read(void *p,unsigned char *b,size_t n,int timeout) {
    pixma_io_t io=fake_scanner_io(p);return io.read(io.ctx,b,n,timeout);
}
void fake_busy(void *p,int n) {((fake_scanner_t *)p)->busy_starts=n;}
void fake_fail(void *p,int n) {((fake_scanner_t *)p)->fail_at_block=n;}
int fake_errors(void *p) {return ((fake_scanner_t *)p)->errors;}
const char *fake_error(void *p) {return ((fake_scanner_t *)p)->error;}
int fake_closed(void *p) {fake_scanner_t *f=p;return !f->job_open&&!f->session_open;}
void fake_pixels(int dpi,int x,int y,int width,unsigned char *out) {
    for(int i=0;i<width;i++) fake_scanner_pixel(dpi,x+i,y,out+3*i);
}

struct capture {void *fake;FILE *commands,*rgb;int width;};
static int capture_write(void *ctx,const uint8_t *data,size_t n,int timeout) {
    struct capture *c=ctx;
    unsigned char header[4]={n>>24,n>>16,n>>8,n};
    if(fwrite(header,1,4,c->commands)!=4||fwrite(data,1,n,c->commands)!=n) return PIXMA_EIO;
    return fake_write(c->fake,data,n,timeout);
}
static int capture_read(void *ctx,uint8_t *data,size_t n,int timeout) {
    return fake_read(((struct capture *)ctx)->fake,data,n,timeout);
}
static void capture_sleep(void *ctx,int ms) {(void)ctx;(void)ms;}
static int capture_line(void *ctx,int y,const uint8_t *rgb) {
    struct capture *c=ctx;(void)y;
    return fwrite(rgb,3,c->width,c->rgb)!=(size_t)c->width;
}
int reference_scan(int dpi,int x,int y,int w,int h,int calibrate,int busy,
                   const char *commands,const char *rgb) {
    struct capture c={.fake=fake_new(),.commands=fopen(commands,"wb"),.rgb=fopen(rgb,"wb"),.width=w};
    if(!c.fake||!c.commands||!c.rgb) return -100;
    fake_busy(c.fake,busy);
    pixma_io_t io={capture_write,capture_read,capture_sleep,&c};
    pixma_params_t p={dpi,x,y,w,h,calibrate};
    int result=pixma_scan(&io,&p,capture_line,&c,NULL);
    if(fake_errors(c.fake)||!fake_closed(c.fake)) result=-101;
    fclose(c.commands);fclose(c.rgb);fake_delete(c.fake);return result;
}

static int sink(void *ctx,const uint8_t *b,size_t n) {return fwrite(b,1,n,ctx)==n?0:-1;}
int reference_print(int dpi,int paper,const char *input,int rows,const char *output) {
    FILE *in=fopen(input,"rb"),*out=fopen(output,"wb");
    if(!in||!out) return -100;
    bjl_job_t j;
    if(bjl_job_init(&j,dpi==300?BJL_MODE_300DPI:BJL_MODE_600DPI,paper,sink,out)) return -101;
    bjl_render_t r;
    if(bjl_render_init(&r,&j)) return -102;
    int width=j.width+j.left;
    uint8_t *line=malloc(width*3);
    if(!line) return -103;
    memset(line,255,width*3);
    bjl_emit_start_job(&j);bjl_emit_mode_shift(&j);bjl_page_begin(&j);
    bjl_render_page(&r,width,rows+j.top,3);
    int result=0;
    for(int y=0;y<rows;y++) {
        if(fread(line+j.left*3,3,j.width,in)!=(size_t)j.width) {result=-104;break;}
        bjl_render_line(&r,y+j.top,line);
    }
    bjl_page_end(&j);bjl_emit_end_job(&j);
    if(j.err) result=j.err;
    free(line);bjl_render_free(&r);bjl_job_free(&j);fclose(in);fclose(out);
    return result;
}
