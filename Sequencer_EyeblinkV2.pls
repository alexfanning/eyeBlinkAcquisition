            SET    1.000,1,0           ; Get rate & scaling OK

            VAR    V45,LoopC=0     ; Define variable for section loops
            VAR    V46,RampC=0     ; Define variable for ramp loops
            VAR    V47,DelayC=0    ; Define variable for delay loops
            VAR    V48,Delay2=0    ;  and another one
            VAR    V49,Delay3=0    ;  and another one
            VAR    V50,Delay4=0    ;  and another one
            VAR    V51,Delay5=0    ;  and another one

;------------------------------------------------------------------------------
; DOT-MASK DIGOUT format for bits 15..8:
; [DO15 DO14 DO13 DO12 DO11 DO10 DO9 DO8]
;  1 = set high, 0 = set low, . = leave unchanged
;
; Mapping:
;   DO15 = Camera
;   DO13 = Air puff
;   DO12 = Tone bit
;   DO11 = Stimulator
;   DO8  = Neuropixels pulse
;------------------------------------------------------------------------------

; Ensure the sequencer starts in a loop waiting for keys
START:      NOP                     ; No operation
            DELAY  s(0.001)         ; Tiny delay
            JUMP   START            ; Loop back until a Key interrupts

;-------------------------
; Init all low
;-------------------------
AllLow: 'I      DIGOUT [00000000]
                  MARK 0
                  DELAY s(0.498)-1
                  JUMP START

;-------------------------
; Spontaneous NPX on
;-------------------------
NPXon: 'N       DIGOUT [.......1]
                  MARK 1
                  JUMP START

;-------------------------
; Spontaneous NPX off
;-------------------------
NPXoff: 'n       DIGOUT [.......0]
                  MARK 2
                  JUMP START

;-------------------------
; Start stim and NPX
;-------------------------
StimOn: 'S      DIGOUT [.......1]
                  MARK 3
                  JUMP START

;-------------------------
; Spontaneous camera only (20 s)
; Raises camera TTL (DO15) high for 20 s then lowers it.
; Triggered by SampleKey("B") at t=580 s of the spontaneous period.
;-------------------------
SpontCam: 'B    DIGOUT [1.......]
                  MARK 10
                  DELAY  s(20.0)-1
                  DIGOUT [0.......]
                  MARK 11
                  JUMP START

;-------------------------
; Test air puff only (no camera, no tone, no stim)
; Press once per test puff during setup.
;-------------------------
TestPuff: 'A    DIGOUT [..1.....]
                  MARK 12
                  DELAY  s(0.018)-1
                  DIGOUT [..0.....]
                  MARK 13
                  JUMP START

;-------------------------
; Paired trials
;-------------------------
Trial: 'P       DIGOUT [1.......]
                  MARK 4
                  DELAY  s(0.198)-1
                  DIGOUT [1...1...]
                  MARK 5
                  DELAY  s(0.048)-1
                  DIGOUT [1...0...]
                  MARK 6
                  DELAY  s(0.248)-1
                  DIGOUT [1.1.....]
                  MARK 7
                  DELAY  s(0.018)-1
                  DIGOUT [1.0.....]
                  MARK 8
                  DELAY  s(1.278)-1
                  DIGOUT [0.0..0..]
                  MARK 9
                  JUMP START

;-------------------------
; CS-only trials
;-------------------------

Csonly: 'C      DIGOUT [1.......]
                  MARK 4
                  DELAY  s(0.198)-1
                  DIGOUT [1...1...]
                  MARK 5
                  DELAY  s(0.048)-1
                  DIGOUT [1...0...]
                  MARK 6
                  DELAY  s(0.248)-1
                  MARK 7
                  DELAY  s(0.018)-1
                  MARK 8
                  DELAY  s(1.278)-1
                  DIGOUT [0.0..0..]
                  MARK 9
                  JUMP START
