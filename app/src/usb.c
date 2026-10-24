/* Application USB device driver for the controller at 0xc0780000.
 *
 * This is a trimmed descendant of the bootloader's src/usb.c: the enumeration
 * and EP0 control machinery is kept verbatim (it is the hardware-proven part),
 * but the firmware-upload path (BEGIN/DANGEROUS_WRITE and the bulk receiver)
 * is removed. The app keeps only the management requests fwctl needs to
 * reflash it (INFO, STAY, REBOOT) plus a vendor control-IN that drains the
 * debug log. The config descriptor advertises the printer-class bulk endpoints
 * so the device enumerates identically; the bulk OUT endpoint (EP1) stays
 * armed and feeds a BJL stream to the open print receiver while a USB print
 * transfer is in progress (print_is_active). Other data is discarded, so a
 * host's generic printer driver can never stall on this interface.
 *
 * Interfaces 2 and 3 are IPP-over-USB (class 7/1/4) for driverless scanning;
 * their bulk endpoints are handed to ippusb.c (see escl.h). */
#include "app.h"
#define IN(n) (USB+0x900+(n)*0x20)
#define OUT(n) (USB+0xb00+(n)*0x20)
#define ENABLE 0x80000000u
#define CNAK 0x04000000u
#define ACTIVE 0x8000u
#define STALL 0x200000u

static uint8_t setup_buffer[64] ALIGNED;
static uint8_t control_buffer[256] ALIGNED;
static uint8_t bulk_buffer[512] ALIGNED;
static unsigned max_packet, tx_length, tx_position;
static unsigned configuration;
static bool need_zlp, reboot_status_ack, bulk_armed;
enum control_state { IDLE, DATA_IN, STATUS_IN, STATUS_OUT };
static enum control_state state;

#define PRODUCT "MG2550s App"
#define PID_LOW 0xfe
#define PID_HIGH 0x12
static const uint8_t device_descriptor[]={18,1,0,2,0,0,0,64,0xa9,4,PID_LOW,PID_HIGH,0,2,1,2,3,1};
static const uint8_t qualifier[]={10,6,0,2,0,0,0,64,1,0};
static uint16_t le16(const uint8_t *p) { return p[0]|((uint16_t)p[1]<<8); }

/* IPP-USB endpoints per interface (2, 3) and each IN endpoint's dedicated TX
 * FIFO; FIFO 1 belongs to the printer interface's EP2 IN. Low endpoint
 * numbers are reused in the unused direction to keep the highest at 3. */
static const uint8_t ipp_out[IPPUSB_INTERFACES]={2,3}, ipp_in[IPPUSB_INTERFACES]={1,3};
static const uint8_t ipp_fifo[IPPUSB_INTERFACES]={2,3};

/* Max packet is 512 (high speed) or 64 (full speed): both powers of two. */
static unsigned packet_count(unsigned length)
{ return length?(length+max_packet-1)>>(max_packet==512?9:6):1; }

unsigned usb_max_packet(void) { return max_packet; }

bool usb_ipp_receive(unsigned conn, uint8_t *packet)
{
    if(!configuration || conn>=IPPUSB_INTERFACES) return false;
    unsigned n=ipp_out[conn];
    wr(OUT(n)+8,0xffffffff); wr(USB+0x880+n*4,7);
    wr(OUT(n)+0x10,(1u<<19)|max_packet);
    wr(OUT(n)+0x14,(uintptr_t)packet);
    modify(OUT(n),STALL,ACTIVE|ENABLE|CNAK);
    return true;
}

bool usb_ipp_send(unsigned conn, const uint8_t *data, unsigned length)
{
    if(!configuration || conn>=IPPUSB_INTERFACES) return false;
    unsigned n=ipp_in[conn];
    wr(IN(n)+8,0xffffffff); wr(USB+0x840+n*4,7);
    wr(IN(n)+0x10,(packet_count(length)<<19)|length);
    wr(IN(n)+0x14,(uintptr_t)data);
    modify(IN(n),STALL,ACTIVE|ENABLE|CNAK);
    return true;
}

static void ipp_endpoints(uint32_t clear, uint32_t set)
{
    for(unsigned c=0;c<IPPUSB_INTERFACES;++c) { modify(OUT(ipp_out[c]),clear,set); modify(IN(ipp_in[c]),clear,set); }
}

/* Printer-class requests name the interface either as wIndex (host tools) or
 * as interface<<8|alternate (USB printer class 1.1). Returns 0 if neither. */
static unsigned printer_interface(uint16_t index)
{
    unsigned n=index<256?index:(index&255)?0:index>>8u;
    return n>=1 && n<=1+IPPUSB_INTERFACES?n:0;
}

static bool ipp_endpoint(uint16_t index)
{
    for(unsigned c=0;c<IPPUSB_INTERFACES;++c)
        if(index==ipp_out[c] || index==(0x80u|ipp_in[c])) return true;
    return false;
}

/* Arm the printer-class bulk OUT endpoint (EP1) for one max-packet into
 * bulk_buffer, unless it is already armed: an armed transfer is never
 * reprogrammed. Always armed while configured; see usb_poll for discard. */
static void arm_bulk(void)
{
    if(!configuration || bulk_armed) return;
    bulk_armed=true;
    wr(OUT(1)+8,0xffffffff); wr(USB+0x884,7);
    wr(OUT(1)+0x10,(1u<<19)|max_packet);
    wr(OUT(1)+0x14,(uintptr_t)bulk_buffer);
    modify(OUT(1),STALL,ACTIVE|ENABLE|CNAK);
}

static void arm_setup(void)
{
    wr(OUT(0)+0x10,0x60080040); wr(OUT(0)+0x14,(uintptr_t)setup_buffer);
    modify(OUT(0),0,ENABLE);
    wr(USB+0x880,0x100f);
}

static void arm_status_out(void)
{
    state=STATUS_OUT;
    arm_setup(); modify(OUT(0),STALL,ENABLE|CNAK);
}

static void send_packet(unsigned count, const uint8_t *data)
{
    wr(IN(0)+8,0xffffffff); wr(USB+0x840,7);
    wr(IN(0)+0x10,(1u<<19)|count); wr(IN(0)+0x14,(uintptr_t)data);
    modify(IN(0),STALL,ENABLE|CNAK);
}

static void next_packet(void)
{
    unsigned left=tx_length-tx_position;
    if(left) {
        unsigned n=left>64?64:left;
        send_packet(n,control_buffer+tx_position); tx_position+=n;
    } else if(need_zlp) {
        need_zlp=false; send_packet(0,control_buffer);
    } else arm_status_out();
}

static void reply(unsigned length, unsigned requested)
{
    if(length>requested) length=requested;
    tx_length=length; tx_position=0;
    need_zlp=(length<requested && !(length&63));
    state=DATA_IN; arm_setup();
    if(!length) { need_zlp=false; send_packet(0,control_buffer); }
    else next_packet();
}

static void ack(void)
{ state=STATUS_IN; arm_setup(); send_packet(0,control_buffer); }

static void stall(void)
{
    state=IDLE;
    modify(IN(0),0,STALL); modify(OUT(0),0,STALL); arm_setup();
}

static unsigned config_descriptor(bool other_speed)
{
    unsigned mps=other_speed?(max_packet==512?64:512):max_packet;
    /* Empty vendor interface 0 preserves printer interface 1 for host tools.
     * Interfaces 2 and 3 are IPP-USB; endpoints must match ipp_out/ipp_in.
     * Each carries the mandatory IPP USB Device Info Descriptor (type 21h):
     * release 1, one IPP Basic Capabilities descriptor with Print (bit 1),
     * Scan (bit 2) and Any HTTP/1.1 over USB (bit 5), so hosts may send eSCL,
     * not only IPP; versions and UUID come from IPP (string indexes 0). */
#define IPP_USB_DEVICE_INFO 10,0x21,1,1,0,4,0x13,0x00,0,0
    const uint8_t config[]={9,2,107,0,4,1,0,0xc0,1,
        9,4,0,0,0,0xff,0,0,0,
        9,4,1,0,2,7,1,2,0,
        7,5,1,2,0,0,0, 7,5,0x82,2,0,0,0,
        9,4,2,0,2,7,1,4,0, IPP_USB_DEVICE_INFO,
        7,5,2,2,0,0,0, 7,5,0x81,2,0,0,0,
        9,4,3,0,2,7,1,4,0, IPP_USB_DEVICE_INFO,
        7,5,3,2,0,0,0, 7,5,0x83,2,0,0,0};
    memcpy(control_buffer,config,sizeof(config));
    control_buffer[1]=other_speed?7:2;
    for(unsigned i=0;i<sizeof(config);i+=control_buffer[i])
        if(control_buffer[i+1]==5) { control_buffer[i+4]=(uint8_t)mps; control_buffer[i+5]=(uint8_t)(mps>>8); }
    return sizeof(config);
}

static unsigned string_descriptor(unsigned index)
{
    if(index==0) { control_buffer[0]=4; control_buffer[1]=3; control_buffer[2]=9; control_buffer[3]=4; return 4; }
    const char *s;
    if(index==1) s="OpenFW";
    else if(index==2) s=PRODUCT;
    else if(index==3) s="OPEN0001";
    else return 0;
    unsigned n=(unsigned)strlen(s);
    control_buffer[0]=(uint8_t)(2+2*n); control_buffer[1]=3;
    for(unsigned i=0;i<n;++i) { control_buffer[2+2*i]=(uint8_t)s[i]; control_buffer[3+2*i]=0; }
    return 2+2*n;
}

static unsigned device_id(void)
{
    /* IEEE-1284 device ID. Status is fixed ready; the app does no updates.
     * CMD/URF announce an AirPrint (IPP-USB) device, which steers macOS to the
     * IPP-USB interfaces instead of a generic USB printer driver. The IPP
     * answer (ipp.c) states that jobs are not accepted yet. */
    static const char body[]="MFG:OpenFW;MDL:MG2550s;CMD:URF;CLS:PRINTER;DES:" PRODUCT
                             ";VER:0.3;STA:80;URF:W8,SRGB24,CP1,RS300,V1.4,DM1;";
    unsigned n=sizeof(body)-1;
    control_buffer[0]=(uint8_t)((n+2)>>8); control_buffer[1]=(uint8_t)(n+2);
    memcpy(control_buffer+2,body,n);
    return n+2;
}

static void setup_request(void)
{
    uint8_t type=setup_buffer[0], request=setup_buffer[1];
    uint16_t value=le16(setup_buffer+2), index=le16(setup_buffer+4), length=le16(setup_buffer+6);
    reboot_status_ack=false;
    /* Abort any old IN transfer/FIFO on a new SETUP. */
    modify(IN(0),ENABLE|STALL,0); modify(OUT(0),STALL,0);
    wr(USB+0x10,0x20); while(rd(USB+0x10)&0x20) {}
    wr(IN(0)+8,0xffffffff);
    if(scan_page_busy() && type==0x40 &&
       ((request>=APP_REQUEST_SCAN && request<=APP_REQUEST_SCAN_FULLCAP) ||
        request==APP_REQUEST_HOME || request==APP_REQUEST_MOTOR_JOG ||
        request==APP_REQUEST_PRINT_TEST || request==APP_REQUEST_PRINT_BEGIN)) {
        stall();
    } else if(type==0x40 && request==APP_REQUEST_PAGE_START && !length) {
        if(scan_page_request(value,index)) ack(); else stall();
    } else if(type==0xc0 && request==APP_REQUEST_PAGE_STATUS && !value && !index) {
        reply(scan_page_status(control_buffer),length);
    } else if(type==0xc0 && request==APP_REQUEST_PAGE_DATA) {
        unsigned n=scan_page_data(control_buffer,index,value,
                                 length>sizeof(control_buffer)?sizeof(control_buffer):length);
        if(n) reply(n,length); else stall();
    } else if(type==0x40 && request==APP_REQUEST_PAGE_ACK && !index && !length) {
        if(scan_page_ack(value)) ack(); else stall();
    } else if(scanner_motor_busy() && type==0x40 &&
       (request==APP_REQUEST_PRINT_TEST || request==APP_REQUEST_PRINT_BEGIN)) {
        stall();
    } else if(type==0xc0 && request==APP_REQUEST_HOME_STATUS && !value && !index) {
        reply(scanner_home_status(control_buffer),length);
    } else if(type==0x40 && request==APP_REQUEST_HOME && !value && !index && !length) {
        if(scanner_home_request()) ack(); else stall();
    } else if(type==0x40 && request==APP_REQUEST_MOTOR_STOP && !value && !index && !length) {
        scan_page_cancel(); scanner_motor_cancel(); scanner_lamp_off(); ack();
    } else if(type==0xc0 && request==REQUEST_INFO && !value && !index) {
        reply(service_info(control_buffer),length);
    } else if(type==0xc0 && request==APP_REQUEST_LOG && !value && !index) {
        reply(dbg_fill(control_buffer,length>sizeof(control_buffer)?sizeof(control_buffer):length),length);
    } else if(type==0x40 && request==APP_REQUEST_SCAN_BURST && !length && (value&255)>=1 && (value&255)<=8
              && ((value>>8)&15)>=1 && ((value>>8)&15)<=8 && (index&255)>=1 && (index&255)<=13
              && !scanner_motor_busy() && !scan_page_busy()) {
        unsigned step=value>>12, offset=step>=8?0u:0xffu-0x20u*step;
        scanner_burst_request(value&255,(value>>8)&15,index&255,(index>>8)*8u,offset); ack();
    } else if(type==0x40 && request==APP_REQUEST_SCAN_SPEED && !index && !length) {
        scan_page_set_speed(value*100u); ack();
    } else if(type==0x40 && request==APP_REQUEST_HOME_SPEED && !index && !length) {
        if(scanner_motor_set_return_speed(value*100u)) ack(); else stall();
    } else if(type==0x40 && request==APP_REQUEST_LINE_AFE && !length) {
        if(scanner_line_set_afe(value,index)) ack(); else stall();
    } else if(type==0x40 && request==APP_REQUEST_LINE_PROBE && !length) {
        if(scanner_line_probe_config(value,index)) ack(); else stall();
    } else if(type==0xc0 && request==APP_REQUEST_LINE_DUMP) {
        unsigned n=scanner_line_dump(control_buffer,(uint32_t)index<<16|value,length>sizeof(control_buffer)?sizeof(control_buffer):length);
        if(n) reply(n,length); else stall();
    } else if(type==0xc0 && request==APP_REQUEST_BURST_DUMP && !index) {
        reply(scanner_burst_dump(control_buffer,value,length>sizeof(control_buffer)?sizeof(control_buffer):length),length);
    } else if(type==0x40 && request==APP_REQUEST_PAGE_PARK && !index && !length) {
        if(scan_page_park((uint32_t)value*256u)) ack(); else stall();
    } else if(type==0xc0 && request==APP_REQUEST_SCAN_DUMP && !index) {
        reply(scanner_dump(control_buffer,value,length>sizeof(control_buffer)?sizeof(control_buffer):length),length);
    } else if(type==0x40 && request==APP_REQUEST_SCAN && !value && !index && !length) {
        scanner_request(); ack();
    } else if(type==0x40 && request==APP_REQUEST_SCAN_PROBE && !value && !index && !length) {
        scanner_probe_request(); ack();
    } else if(type==0x40 && request==APP_REQUEST_SCAN_BRINGUP && !value && !index && !length) {
        scanner_bringup_request(); ack();
    } else if(type==0x40 && request==APP_REQUEST_SCAN_CAPTURE && !value && !index && !length) {
        scanner_capture_request(); ack();
    } else if(type==0x40 && request==APP_REQUEST_SCAN_ENABLE && !value && !index && !length) {
        scanner_enable_request(); ack();
    } else if(type==0x40 && request==APP_REQUEST_SCAN_FIELD && !value && !index && !length) {
        scanner_field_request(); ack();
    } else if(type==0x40 && request==APP_REQUEST_SCAN_LDMAC && !value && !index && !length) {
        scanner_ldmac_request(); ack();
    } else if(type==0x40 && request==APP_REQUEST_SCAN_DMASETUP && !value && !index && !length) {
        scanner_dmasetup_request(); ack();
    } else if(type==0x40 && request==APP_REQUEST_SCAN_FULLCAP && !index && !length) {
        scanner_fullcap_request(value); ack();
    } else if(type==0x40 && request==APP_REQUEST_PRINT_TEST && !value && !index && !length) {
        print_selftest(); ack();
    } else if(type==0x40 && request==APP_REQUEST_PRINT_BEGIN && !length) {
        print_bulk_begin((unsigned)value | ((unsigned)index<<16)); arm_bulk(); ack();
    } else if(type==0x40 && request==APP_REQUEST_BTN && !value && !index && !length) {
        buttons_request(); ack();
    } else if(type==0x40 && request==APP_REQUEST_MOTOR_JOG && !index && !length && value<=1u) {
        if(scanner_motor_request(value)) ack(); else stall();
    } else if(type==0x40 && request==REQUEST_STAY && !value && !index && !length) {
        if(service_stay()) ack(); else stall();
    } else if(type==0x40 && request==REQUEST_REBOOT && !index && !length) {
        if(service_reboot(value)) { reboot_status_ack=true; ack(); } else stall();
    } else if(type==0x80 && request==6) {
        unsigned n=0, desc=value>>8, id=value&255;
        if(desc==1 && id==0) { n=sizeof(device_descriptor); memcpy(control_buffer,device_descriptor,n); }
        else if(desc==2 && id==0) n=config_descriptor(false);
        else if(desc==3) n=string_descriptor(id);
        else if(desc==6 && id==0) { n=sizeof(qualifier); memcpy(control_buffer,qualifier,n); }
        else if(desc==7 && id==0) n=config_descriptor(true);
        if(n) reply(n,length); else stall();
    } else if(type==0 && request==5 && value<128 && index==0 && length==0) {
        /* DCFG address must be loaded before the status-IN job on this core. */
        modify(USB+0x800,0x7f0,(uint32_t)value<<4); ack();
    } else if(type==0 && request==9 && value<=1 && index==0 && length==0) {
        configuration=value; bulk_armed=false;
        ippusb_reset();
        if(configuration) { modify(OUT(1),0,ACTIVE); modify(IN(2),0,ACTIVE); ipp_endpoints(0,ACTIVE); arm_bulk(); }
        else { modify(OUT(1),ACTIVE|ENABLE,0); modify(IN(2),ACTIVE|ENABLE,0); ipp_endpoints(ACTIVE|ENABLE,0); }
        ack();
    } else if(type==0x80 && request==8 && !value && !index && length==1) {
        control_buffer[0]=(uint8_t)configuration; reply(1,length);
    } else if((type==0x80 || type==0x81 || type==0x82) && request==0 && !value && length==2) {
        control_buffer[0]=type==0x80?1:0; control_buffer[1]=0; reply(2,length);
    } else if(type==0x81 && request==10 && !value && index<2+IPPUSB_INTERFACES && length==1) {
        control_buffer[0]=0; reply(1,length);
    } else if(type==1 && request==11 && !value && index<2+IPPUSB_INTERFACES && !length) {
        /* Selecting an IPP-USB interface restarts its stream at DATA0. */
        if(index>=2) {
            modify(OUT(ipp_out[index-2]),0,0x10000000); modify(IN(ipp_in[index-2]),0,0x10000000);
            ippusb_reset_conn(index-2);
        }
        ack();
    } else if(type==0xa1 && request==0 && value==0 && printer_interface(index)) {
        reply(device_id(),length);
    } else if(type==0xa1 && request==1 && !value && printer_interface(index) && length==1) {
        control_buffer[0]=0x18; reply(1,length);
    } else if(type==0x21 && request==2 && !value && printer_interface(index) && !length) {
        if(printer_interface(index)>=2) ippusb_reset_conn(printer_interface(index)-2);
        ack();
    } else if(type==2 && request==1 && !value && !length && (index==1 || index==0x82)) {
        modify(index==1?OUT(1):IN(2),STALL,0x10000000); if(index==1) arm_bulk(); ack();
    } else if(type==2 && request==1 && !value && !length && ipp_endpoint(index)) {
        modify(index&0x80?IN(index&15):OUT(index),STALL,0x10000000); ack();
    } else stall();
}

static void bus_reset(void)
{
    configuration=0; state=IDLE; bulk_armed=false;
    ippusb_reset();
    modify(USB+0x800,0x7f0,0);
    for(unsigned n=0;n<4;++n) { wr(IN(n),0); wr(OUT(n),0); wr(IN(n)+8,0xffffffff); wr(OUT(n)+8,0xffffffff); }
    wr(USB+0x10,0x430); while(rd(USB+0x10)&0x30) {}
}

static void enumerated(void)
{
    max_packet=(rd(USB+0x808)&6)==0?512:64;
    wr(IN(0),0x08008000); wr(OUT(0),0x08008000);
    wr(OUT(1),0x18080000|max_packet); wr(IN(2),0x18480000|max_packet);
    for(unsigned c=0;c<IPPUSB_INTERFACES;++c) {
        wr(OUT(ipp_out[c]),0x18080000|max_packet);
        wr(IN(ipp_in[c]),0x18080000|((uint32_t)ipp_fifo[c]<<22)|max_packet);
    }
    wr(USB+0x840,7); wr(USB+0x880,0x100f); wr(USB+0x884,7);
    arm_setup();
}

void usb_disconnect(void)
{ modify(USB+0x804,0,2); modify(0xc0c88024,0x10000,0); }

void usb_init(void)
{
    wr(0xc0c88064,1); wr(0xc0c88060,1);
    modify(0xc07f0194,2,0); modify(0xc07f0194,1,0);
    wr(0xc0c880a8,0x10a); delay_ticks(0x1e0);
    wr(0xc0c88058,0); delay_ticks(0x1e0);
    wr(0xc0c88078,0); wr(0xc0c88064,0); wr(0xc0c88060,0);
    while(!(rd(0xc0c88024)&0x100)) { leds_tick(); }
    modify(0xc07f0194,0,2); modify(0xc07f0194,0,1);
    while(!(rd(USB+0x10)&ENABLE)) { leds_tick(); }
    usb_disconnect();
    wr(USB+0x10,1); while(rd(USB+0x10)&1) {}
    while(!(rd(USB+0x10)&ENABLE)) {}
    wr(USB+0x804,2); wr(USB+8,0x2a); wr(USB+0xc,0x2400); wr(USB+0x800,4);
    wr(USB+0x24,0x127); wr(USB+0x28,0x00100127); wr(USB+0x104,0x00800137);
    /* 128-word TX FIFOs 2 and 3 for the IPP-USB IN endpoints, after FIFO 1.
     * Needs 0x2b7 words of FIFO RAM in total; not yet confirmed on hardware. */
    wr(USB+0x108,0x008001b7); wr(USB+0x10c,0x00800237);
    wr(USB+0x14,0xffffffff); wr(USB+0x18,0xc3000);
    for(unsigned n=0;n<4;++n) {
        wr(IN(n)+8,0xffffffff); wr(OUT(n)+8,0xffffffff);
        wr(USB+0x840+n*4,0); wr(USB+0x880+n*4,0);
    }
    wr(USB+0x83c,0x30005); modify(0xc0c880a8,0x100,0); modify(USB+0x800,3,0);
    configuration=0; max_packet=64; state=IDLE;
    delay_ticks(0x17700);
    /* ASIC PHY connection enable, distinct from the core soft-disconnect. */
    modify(0xc0c88024,0,0x10000); modify(USB+0x804,2,0);
}

void usb_poll(void)
{
    uint32_t events=rd(USB+0x14)&rd(USB+0x18);
    if(events) wr(USB+0x14,events);
    if(events&0x1000) bus_reset();
    if(events&0x2000) enumerated();
    uint32_t out=rd(OUT(0)+8)&rd(USB+0x880);
    uint32_t in=rd(IN(0)+8)&rd(USB+0x840);
    if(out) wr(OUT(0)+8,out);
    if(in) wr(IN(0)+8,in);
    /* With several host clients (macOS ippusbd next to fwctl) the next SETUP
     * can arrive in the same poll as the previous status stage. Acknowledge a
     * reboot first: setup_request() clears the flag, losing the reboot. */
    if((in&1) && state==STATUS_IN && reboot_status_ack) { reboot_status_ack=false; service_reboot_ack(); }
    if(out&8) { setup_request(); return; }
    if(in&1) {
        if(state==DATA_IN) next_packet();
        else if(state==STATUS_IN) {
            if(reboot_status_ack) { reboot_status_ack=false; service_reboot_ack(); }
            state=IDLE; arm_setup();
        }
    }
    if((out&1) && state==STATUS_OUT) {
        state=IDLE; arm_setup();
    }
    /* Bulk OUT (EP1): feed a BJL packet to an active print receiver, else
     * discard it (print_bulk_feed ignores data outside a transfer). */
    uint32_t bulk=rd(OUT(1)+8)&rd(USB+0x884);
    if(bulk) wr(OUT(1)+8,bulk);
    if((bulk&1) && configuration) {
        unsigned remaining=rd(OUT(1)+0x10)&0x7ffff;
        bulk_armed=false;
        if(remaining<=max_packet) print_bulk_feed(bulk_buffer,max_packet-remaining);
        arm_bulk();
    }
    /* IPP-USB bulk endpoints: completions go to the HTTP transport. */
    for(unsigned c=0;c<IPPUSB_INTERFACES;++c) {
        unsigned o=ipp_out[c], i=ipp_in[c];
        uint32_t rx=rd(OUT(o)+8)&rd(USB+0x880+o*4), tx=rd(IN(i)+8)&rd(USB+0x840+i*4);
        if(rx) wr(OUT(o)+8,rx);
        if(tx) wr(IN(i)+8,tx);
        if((rx&1) && configuration) {
            unsigned remaining=rd(OUT(o)+0x10)&0x7ffff;
            ippusb_received(c,remaining<=max_packet?max_packet-remaining:0);
        }
        if((tx&1) && configuration) ippusb_sent(c);
    }
}
