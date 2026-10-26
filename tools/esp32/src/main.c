/* ESP32-C3 USB serial -> preloaded I2C slave bridge. No printer EEPROM writes.
 * Default/finished state: GPIO4/5 input-only, with internal pulls disabled. */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/gpio.h"
#include "driver/i2c_slave.h"
#include "driver/usb_serial_jtag.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "hal/i2c_ll.h"
#include "protocol.h"
#include "wiring.h"
#include "sniff.h"

static gpio_num_t sda=GPIO_NUM_5,scl=GPIO_NUM_4;
static struct wiring wiring;
static volatile bool probing;
static const esp_partition_t *partition;
static i2c_slave_dev_handle_t slave;
static QueueHandle_t events;
static uint32_t image_size,image_crc;
static unsigned offer=OFFER_APPLICATION,command_id;
static const uint8_t *image_data;
static esp_partition_mmap_handle_t image_mapping;
static uint8_t response[FRAME_BYTES];
static volatile bool result_pending;
static volatile uint32_t selected=HEADER_REQUEST;
static volatile uint32_t rx_events,read_events,dropped_events;
static volatile uint32_t frames_sent,previous_tx_left;
static int64_t last_activity;
struct event { uint32_t type,value; };

/* The image remains mapped and immutable while armed. Build before the read
 * address arrives, then fill the hardware FIFO directly in the ISR. The first
 * physical test showed that a task/ringbuffer response was not consumed before
 * the Canon master retried. No flash API, lock or task wake is used on reads. */
static void prepare_response(uint32_t offset)
{
    if(probing) { wiring_probe_reply(response); return; }
    memset(response,0xff,sizeof(response)); put32(response,offset);
    if(offset==HEADER_REQUEST) {
        put32(response+4,BRIDGE_MAGIC); put32(response+8,1); put32(response+12,image_size);
        put32(response+16,image_crc); put32(response+20,command_id); put32(response+24,result_pending?0:offer);
    } else if(offset<image_size && offset%FRAME_DATA==0) {
        unsigned n=image_size-offset; if(n>FRAME_DATA) n=FRAME_DATA;
        memcpy(response+4,image_data+offset,n);
    } else memset(response,0,sizeof(response));
    put32(response+28,recovery_crc32(response,28));
}

static void fill_fifo(void)
{
    i2c_dev_t *hw=I2C_LL_GET_HW(0);
    i2c_ll_txfifo_rst(hw);
    i2c_ll_write_txfifo(hw,response,sizeof(response));
}

static void say(const char *s)
{ usb_serial_jtag_write_bytes(s,strlen(s),pdMS_TO_TICKS(1000)); }
static void float_pins(void)
{
    gpio_reset_pin(GPIO_NUM_4); gpio_reset_pin(GPIO_NUM_5);
    gpio_set_direction(GPIO_NUM_4,GPIO_MODE_INPUT); gpio_set_direction(GPIO_NUM_5,GPIO_MODE_INPUT);
    gpio_set_pull_mode(GPIO_NUM_4,GPIO_FLOATING); gpio_set_pull_mode(GPIO_NUM_5,GPIO_FLOATING);
}

/* Main task owns controller lifetime; deletion disables its ISR first. */
static void disarm(void)
{
    if(slave) { i2c_del_slave_device(slave); slave=NULL; }
    float_pins(); xQueueReset(events);
}

static bool received(i2c_slave_dev_handle_t h,const i2c_slave_rx_done_event_data_t *data,void *context)
{
    (void)h; (void)context; BaseType_t wake=pdFALSE;
    ++rx_events;
    if(probing) {
        enum wiring_event kind=wiring_decode(data->buffer,data->length);
        if(kind==WIRING_HEADER) { prepare_response(HEADER_REQUEST); fill_fifo(); }
        struct event e={10u+(unsigned)kind,
                        kind==WIRING_RESULT?get32(data->buffer+1):0};
        if(xQueueSendFromISR(events,&e,&wake)!=pdTRUE) ++dropped_events;
        return wake==pdTRUE;
    }
    if(data->length==5) {
        if(data->buffer[0]==REQUEST_FRAME) {
            previous_tx_left=I2C_LL_GET_HW(0)->sr.tx_fifo_cnt;
            selected=get32(data->buffer+1); prepare_response(selected); fill_fifo();
        }
        else if(data->buffer[0]==REPORT_RESULT) {
            /* Stop advertising an armed image immediately. The printer may
             * re-probe after failure before the worker's delayed pin release. */
            result_pending=true;
            struct event e={2,get32(data->buffer+1)};
            if(xQueueSendFromISR(events,&e,&wake)!=pdTRUE) ++dropped_events;
        }
    }
    return wake==pdTRUE;
}

static bool requested(i2c_slave_dev_handle_t h,const i2c_slave_request_event_data_t *data,void *context)
{
    (void)h; (void)data; (void)context; BaseType_t wake=pdFALSE;
    ++read_events;
    ++frames_sent; /* All 32 bytes were preloaded by the preceding write. */
    if(probing) {
        struct event e={10u+WIRING_READ,0};
        if(xQueueSendFromISR(events,&e,&wake)!=pdTRUE) ++dropped_events;
    }
    i2c_ll_slave_clear_stretch(I2C_LL_GET_HW(0));
    return wake==pdTRUE;
}

static void service(void)
{
    struct event e;
    while(xQueueReceive(events,&e,0)) {
        if(!slave) continue;
        last_activity=esp_timer_get_time();
        if(probing) wiring_observe(&wiring,(enum wiring_event)(e.type-10),e.value);
        else if(e.type==2) {
            /* Let the final write's STOP complete before detaching the pins. */
            vTaskDelay(pdMS_TO_TICKS(20)); disarm();
            char line[80]; snprintf(line,sizeof(line),"DONE %02lX FLOATING\n",(unsigned long)e.value); say(line);
        }
    }
}

static bool attach(bool probe)
{
    if(!probe && (wiring.phase!=WIRING_VERIFIED || (!image_size && offer!=OFFER_COMMAND))) return false;
    probing=probe;
    sda=wiring.orientation?GPIO_NUM_4:GPIO_NUM_5;
    scl=wiring.orientation?GPIO_NUM_5:GPIO_NUM_4;
    selected=HEADER_REQUEST;
    result_pending=false;
    rx_events=read_events=dropped_events=frames_sent=0;
    previous_tx_left=0; prepare_response(selected);
    i2c_slave_config_t config={.i2c_port=I2C_NUM_0,.sda_io_num=sda,.scl_io_num=scl,
        .clk_source=I2C_CLK_SRC_DEFAULT,.send_buf_depth=64,.receive_buf_depth=64,
        .slave_addr=RESCUE_ADDRESS,.addr_bit_len=I2C_ADDR_BIT_LEN_7,.intr_priority=1,
        .flags.enable_internal_pullup=false};
    if(i2c_new_slave_device(&config,&slave)!=ESP_OK) { slave=NULL; float_pins(); return false; }
    i2c_slave_event_callbacks_t callbacks={.on_request=requested,.on_receive=received};
    if(i2c_slave_register_event_callbacks(slave,&callbacks,NULL)!=ESP_OK) { disarm(); return false; }
    fill_fifo();
    /* The Canon master consumed zero/one byte with address-match stretching.
     * Our whole reply is already in FIFO, so no SCL stretching is necessary. */
    i2c_ll_slave_enable_scl_stretch(I2C_LL_GET_HW(0),false);
    i2c_ll_slave_clear_stretch(I2C_LL_GET_HW(0));
    i2c_ll_update(I2C_LL_GET_HW(0));
    /* SCL is input-only even while attached: the bridge never drives a clock
     * line. SDA remains the peripheral's open-drain output, never push-pull. */
    gpio_set_direction(scl,GPIO_MODE_INPUT);
    last_activity=esp_timer_get_time();
    return true;
}

static const char *wiring_state(void)
{
    switch(wiring.phase) {
    case WIRING_PROBING: return "PROBING";
    case WIRING_VERIFIED: return "VERIFIED";
    case WIRING_FAILED: return "FAILED";
    default: return "UNVERIFIED";
    }
}

static void report_wiring(void)
{
    char line[160];
    snprintf(line,sizeof(line),"WIRING %s SDA%d SCL%d PROOF=%s FLOATING\n",
             wiring_state(),(int)sda,(int)scl,
             wiring.proof==WIRING_REJECT90?"REJECT90":
             wiring.proof==WIRING_HEADER_READ?"HEADER_READ":"NONE");
    say(line);
}

static void detect(void)
{
    disarm(); wiring_start(&wiring,esp_timer_get_time());
    if(!attach(true)) { wiring.phase=WIRING_FAILED; probing=false; report_wiring(); }
    else say("DETECTING\n");
}

static void probe_poll(void)
{
    if(!probing) return;
    if(dropped_events) { wiring.phase=WIRING_FAILED; wiring.proof=WIRING_NO_PROOF; }
    if(wiring.phase==WIRING_PROBING && esp_timer_get_time()<wiring.deadline) return;
    char line[160];
    snprintf(line,sizeof(line),"PROBE SDA%d SCL%d RX=%lu READ=%lu EXCHANGES=%u DROP=%lu\n",
             (int)sda,(int)scl,(unsigned long)rx_events,(unsigned long)read_events,
             wiring.exchanges,(unsigned long)dropped_events);
    say(line);
    /* Let an in-flight final result write finish before detaching. No images
     * or operational commands were exposed while testing either orientation. */
    vTaskDelay(pdMS_TO_TICKS(20)); disarm();
    bool next=wiring_expire(&wiring,esp_timer_get_time());
    if(next && attach(true)) return;
    if(next) wiring.phase=WIRING_FAILED;
    probing=false; report_wiring();
}

static bool read_exact(uint8_t *buffer,unsigned size)
{
    while(size) {
        int n=usb_serial_jtag_read_bytes(buffer,size,pdMS_TO_TICKS(5000));
        if(n<=0) return false;
        buffer+=n; size-=(unsigned)n;
    }
    return true;
}

static void load_image(unsigned size,unsigned crc,bool maintenance)
{
    image_size=0; offer=maintenance?OFFER_STAGE0:OFFER_APPLICATION; command_id=0;
    if(image_data) { esp_partition_munmap(image_mapping); image_data=NULL; }
    if(size<APP_HEADER || size>APP_CAPACITY || size>partition->size) { say("ERROR SIZE\n"); return; }
    if(esp_partition_erase_range(partition,0,(size+4095)&~4095u)!=ESP_OK) { say("ERROR ERASE\n"); return; }
    say("SEND\n"); uint8_t buffer[1024];
    for(unsigned offset=0;offset<size;) {
        unsigned n=size-offset; if(n>sizeof(buffer)) n=sizeof(buffer);
        if(!read_exact(buffer,n) || esp_partition_write(partition,offset,buffer,n)!=ESP_OK) {
            say("ERROR LOAD FLOATING\n"); return;
        }
        offset+=n;
    }
    const void *mapped; esp_partition_mmap_handle_t mapping;
    if(esp_partition_mmap(partition,0,size,ESP_PARTITION_MMAP_DATA,&mapped,&mapping)!=ESP_OK) {
        say("ERROR MAP\n"); return;
    }
    const uint8_t *bytes=mapped;
    bool valid=recovery_crc32(mapped,size)==crc && get32(bytes+8)+APP_HEADER==size;
    if(maintenance) {
        valid=valid && size<=BOOTLOADER_CAPACITY && get32(bytes)==APP_MAGIC && get32(bytes+4)==APP_VERSION &&
            !(get32(bytes+8)&3) && get32(bytes+12)==0x20010000 && get32(bytes+16)==0 &&
            get32(bytes+24)==0 && get32(bytes+28)==recovery_crc32(bytes,28) &&
            get32(bytes+20)==recovery_crc32(bytes+APP_HEADER,size-APP_HEADER);
    } else valid=valid && app_valid(mapped,size);
    if(!valid) { esp_partition_munmap(mapping); say("ERROR IMAGE\n"); return; }
    image_mapping=mapping; image_data=mapped;
    image_size=size; image_crc=crc; say("LOADED FLOATING\n");
}

void app_main(void)
{
    float_pins();
    usb_serial_jtag_driver_config_t serial={.tx_buffer_size=2048,.rx_buffer_size=4096};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&serial));
    partition=esp_partition_find_first(ESP_PARTITION_TYPE_DATA,0x40,"image");
    if(!partition) { say("ERROR PARTITION\n"); return; }
    events=xQueueCreate(64,sizeof(struct event));
    configASSERT(events);
    say("READY RSC1 AUTOWIRE=1\n"); detect();
    char line[96]; unsigned position=0; uint32_t observed_activity=0;
    for(;;) {
        char c; int count=usb_serial_jtag_read_bytes(&c,1,pdMS_TO_TICKS(10));
        service(); probe_poll(); sniff_poll();
        if(slave && observed_activity!=read_events+rx_events) {
            observed_activity=read_events+rx_events; last_activity=esp_timer_get_time();
        }
        if(slave && esp_timer_get_time()-last_activity>120000000) {
            disarm(); wiring=(struct wiring){0}; say("TIMEOUT FLOATING\n");
        }
        if(count==1 && c=='\n') {
            line[position]=0; position=0;
            if(!strcmp(line,"INFO")) {
                char report[128];
                snprintf(report,sizeof(report),"INFO RSC1 %s AUTOWIRE=1 WIRING=%s\n",
                         probing?"PROBING":slave?"ARMED":"FLOATING",wiring_state());
                say(report);
            }
            else if(!strcmp(line,"STATS")) {
                char report[192];
                snprintf(report,sizeof(report),
                    "STATS %s RX=%lu READ=%lu SENT=%lu DROP=%lu OFFSET=%08lX TXLEFT=%lu SDA=%d SCL=%d\n",
                    probing?"PROBING":slave?"ARMED":"FLOATING",(unsigned long)rx_events,(unsigned long)read_events,
                    (unsigned long)frames_sent,(unsigned long)dropped_events,(unsigned long)selected,
                    (unsigned long)previous_tx_left,
                    gpio_get_level(sda),gpio_get_level(scl));
                say(report);
            }
            else if(!strcmp(line,"SNIFF") && !slave && !probing) {
                /* Passive bus capture on the verified (or default) orientation; never drives a pin. */
                gpio_num_t s_sda=wiring.phase==WIRING_VERIFIED?sda:GPIO_NUM_5, s_scl=wiring.phase==WIRING_VERIFIED?scl:GPIO_NUM_4;
                char report[96];
                if(sniff_start(s_sda,s_scl,say)) { snprintf(report,sizeof(report),"SNIFFING SDA%d SCL%d PASSIVE\n",(int)s_sda,(int)s_scl); say(report); }
                else say("ERROR SNIFF\n");
            }
            else if(!strcmp(line,"STOP")) { if(sniff_active()) sniff_stop(); else say("ERROR NOT_SNIFFING\n"); }
            else if(!strcmp(line,"FLOAT")) {
                disarm(); probing=false; wiring=(struct wiring){0}; say("FLOATING\n");
            }
            else if(!strcmp(line,"DETECT") && (!slave || probing)) {
                if(probing) say("DETECTING\n"); else detect();
            }
            else if(!strcmp(line,"WIRING") && !slave) report_wiring();
            else if(!strcmp(line,"ARM")) {
                if(wiring.phase!=WIRING_VERIFIED) say("ERROR WIRING_UNVERIFIED\n");
                else if(slave) say("ARMED\n");
                else say(attach(false)?"ARMED\n":"ERROR ARM FLOATING\n");
            } else if(!strncmp(line,"COMMAND ",8) && !slave) {
                unsigned command;
                if(wiring.phase!=WIRING_VERIFIED) say("ERROR WIRING_UNVERIFIED FLOATING\n");
                else if(sscanf(line+8,"%u",&command)==1 && command>=1 && command<=4) {
                    offer=OFFER_COMMAND; command_id=command;
                    say(attach(false)?"ARMED\n":"ERROR ARM FLOATING\n");
                } else say("ERROR COMMAND\n");
            } else if(!strncmp(line,"LOADBOOT ",9) && !slave) {
                unsigned size,crc;
                if(wiring.phase!=WIRING_VERIFIED) say("ERROR WIRING_UNVERIFIED FLOATING\n");
                else if(sscanf(line+9,"%u %x",&size,&crc)==2) load_image(size,crc,true);
                else say("ERROR COMMAND\n");
            } else if(!strncmp(line,"LOAD ",5) && !slave) {
                unsigned size,crc;
                if(wiring.phase!=WIRING_VERIFIED) say("ERROR WIRING_UNVERIFIED FLOATING\n");
                else if(sscanf(line+5,"%u %x",&size,&crc)==2) load_image(size,crc,false);
                else say("ERROR COMMAND\n");
            } else say("ERROR COMMAND_OR_ARMED\n");
        } else if(count==1 && c!='\r') {
            if(position<sizeof(line)-1) line[position++]=c; else position=0;
        }
    }
}
