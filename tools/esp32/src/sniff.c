/* Passive I2C bus sniffer for the recovery bridge: both pins stay inputs with
 * pulls disabled, nothing is ever driven. A dedicated task polls the GPIO input
 * register in short bursts with interrupts masked (tens of ns per sample), so
 * 100-400 kHz I2C edges are not missed; between bursts the system runs normally.
 * Level changes go into a ring; the main loop decodes START, bytes with ACK/NAK
 * and STOP and prints one line per transaction over USB serial. */
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_cpu.h"
#include "soc/gpio_reg.h"
#include "sniff.h"

#define RING 4096
#define BURST_CYCLES (160000000/1000*4)   /* 4 ms of sampling per burst at 160 MHz */
struct sample { uint32_t t; uint8_t sda, scl; };   /* microseconds, wraps every 71 minutes */
static struct sample ring[RING];
static volatile uint32_t head, tail, dropped;
static gpio_num_t sniff_sda=GPIO_NUM_5, sniff_scl=GPIO_NUM_4;
static volatile bool active, exited=true;
static TaskHandle_t task;
static bool in_frame; static unsigned bitcount; static uint8_t byte; static uint8_t prev_sda, prev_scl;
static uint8_t frame[64]; static uint8_t acks[64]; static unsigned frame_len; static uint32_t frame_start;
static uint32_t frames, bytes_total;
static void (*emit)(const char *);

static void sampler(void *arg)
{
    uint32_t mask=(1u<<sniff_sda)|(1u<<sniff_scl);
    uint32_t last=REG_READ(GPIO_IN_REG)&mask;
    exited=false;
    while(active) {
        portDISABLE_INTERRUPTS();
        uint32_t start=esp_cpu_get_cycle_count();
        while(esp_cpu_get_cycle_count()-start<BURST_CYCLES) {
            uint32_t in=REG_READ(GPIO_IN_REG)&mask;
            if(in!=last) {
                last=in;
                uint32_t next=(head+1)&(RING-1);
                if(next==tail) { dropped++; continue; }
                ring[head].t=(uint32_t)esp_timer_get_time();
                ring[head].sda=(in>>sniff_sda)&1; ring[head].scl=(in>>sniff_scl)&1;
                head=next;
            }
        }
        portENABLE_INTERRUPTS();
        vTaskDelay(1);                      /* ~1 ms gap: serial, idle and the decoder run here */
    }
    exited=true; vTaskDelete(NULL);
}

bool sniff_start(gpio_num_t sda, gpio_num_t scl, void (*say)(const char *))
{
    if(active) return true;
    sniff_sda=sda; sniff_scl=scl; emit=say;
    head=tail=dropped=0; in_frame=false; frame_len=0; bitcount=0; frames=bytes_total=0;
    gpio_config_t cfg={.pin_bit_mask=(1ULL<<sda)|(1ULL<<scl),.mode=GPIO_MODE_INPUT,
        .pull_up_en=GPIO_PULLUP_DISABLE,.pull_down_en=GPIO_PULLDOWN_DISABLE,.intr_type=GPIO_INTR_DISABLE};
    if(gpio_config(&cfg)!=ESP_OK) return false;
    uint32_t in=REG_READ(GPIO_IN_REG); prev_sda=(in>>sda)&1; prev_scl=(in>>scl)&1;
    active=true;
    if(xTaskCreate(sampler,"sniff",3072,NULL,configMAX_PRIORITIES-1,&task)!=pdPASS) { active=false; return false; }
    return true;
}

static void flush_frame(const char *end)
{
    if(!frame_len && !bitcount) { if(in_frame) { char l[48]; snprintf(l,sizeof(l),"I2C %lu S %s\n",(unsigned long)frame_start,end); emit(l); frames++; } return; }
    char line[64+4*64+32]; int n=snprintf(line,sizeof(line),"I2C %lu S",(unsigned long)frame_start);
    for(unsigned i=0;i<frame_len && n<(int)sizeof(line)-8;i++)
        n+=snprintf(line+n,sizeof(line)-n," %02x%c",frame[i],acks[i]?'n':'a');
    if(bitcount && n<(int)sizeof(line)-16) n+=snprintf(line+n,sizeof(line)-n," [%u bits %02x]",bitcount,byte);
    snprintf(line+n,sizeof(line)-n," %s\n",end); emit(line);
    frames++; bytes_total+=frame_len; frame_len=0; bitcount=0; byte=0;
}

void sniff_poll(void)
{
    if(!active && tail==head) return;
    unsigned budget=4096;
    while(tail!=head && budget--) {
        struct sample s=ring[tail]; tail=(tail+1)&(RING-1);
        if(s.scl && prev_scl && s.sda!=prev_sda) {        /* SDA edge while SCL high */
            if(!s.sda) { if(in_frame) flush_frame("Sr"); in_frame=true; frame_start=s.t; bitcount=0; byte=0; frame_len=0; }
            else { if(in_frame) flush_frame("P"); in_frame=false; }
        } else if(s.scl && !prev_scl && in_frame) {       /* SCL rising: sample SDA */
            if(bitcount<8) { byte=(uint8_t)((byte<<1)|s.sda); bitcount++; }
            else {
                if(frame_len<sizeof(frame)) { frame[frame_len]=byte; acks[frame_len]=s.sda; frame_len++; }
                bitcount=0; byte=0;
                if(frame_len==sizeof(frame)) flush_frame("...");
            }
        }
        prev_sda=s.sda; prev_scl=s.scl;
    }
}

void sniff_stop(void)
{
    if(!active) return;
    active=false;
    for(int i=0;i<50 && !exited;i++) vTaskDelay(1);
    sniff_poll(); if(in_frame) flush_frame("?");
    char line[96]; snprintf(line,sizeof(line),"SNIFF STOPPED FRAMES=%lu BYTES=%lu DROP=%lu\n",
        (unsigned long)frames,(unsigned long)bytes_total,(unsigned long)dropped); emit(line);
}

bool sniff_active(void) { return active; }
