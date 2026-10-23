// Experimental, untested macOS 15+ helper. No screen output is ever registered.
import Foundation
import AVFoundation
import AudioToolbox
import CoreAudio
import ScreenCaptureKit
import CoreMedia
import Darwin

func fail(_ message:String) -> Never { FileHandle.standardError.write(Data((message+"\n").utf8));exit(1) }
func audioDevices() -> [[String:Any]] {
 var address=AudioObjectPropertyAddress(mSelector:kAudioHardwarePropertyDevices,mScope:kAudioObjectPropertyScopeGlobal,mElement:kAudioObjectPropertyElementMain)
 var bytes:UInt32=0;guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject),&address,0,nil,&bytes)==noErr else{return []}
 var ids=[AudioDeviceID](repeating:0,count:Int(bytes)/MemoryLayout<AudioDeviceID>.size)
 guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),&address,0,nil,&bytes,&ids)==noErr else{return []}
 var devices=[[String:Any]]()
 for id in ids {
  var streams=AudioObjectPropertyAddress(mSelector:kAudioDevicePropertyStreams,mScope:kAudioDevicePropertyScopeInput,mElement:kAudioObjectPropertyElementMain);var size:UInt32=0
  if AudioObjectGetPropertyDataSize(id,&streams,0,nil,&size) != noErr || size==0{continue}
  var uid:CFString="" as CFString;var name:CFString="" as CFString;var p=AudioObjectPropertyAddress(mSelector:kAudioDevicePropertyDeviceUID,mScope:kAudioObjectPropertyScopeGlobal,mElement:kAudioObjectPropertyElementMain);var n=UInt32(MemoryLayout<CFString>.size)
  guard AudioObjectGetPropertyData(id,&p,0,nil,&n,&uid)==noErr else{continue};p.mSelector=kAudioObjectPropertyName;_ = AudioObjectGetPropertyData(id,&p,0,nil,&n,&name)
  devices.append(["key":"coreaudio:\(uid)","name":name as String,"kind":"mic","is_default":false,"available":true,"experimental":true])
 }
 return devices
}
// The consumer alone resamples and writes inherited PCM/hardware-clock frames.
func emit(_ samples:[Float],_ position:UInt64,_ timestamp:UInt64){var count=UInt32(samples.count).littleEndian;var position=position.littleEndian;var timestamp=timestamp.littleEndian;var flags=UInt32(0);withUnsafeBytes(of:&count){FileHandle.standardOutput.write(Data($0))};withUnsafeBytes(of:&position){FileHandle.standardOutput.write(Data($0))};withUnsafeBytes(of:&timestamp){FileHandle.standardOutput.write(Data($0))};withUnsafeBytes(of:&flags){FileHandle.standardOutput.write(Data($0))};samples.withUnsafeBytes{FileHandle.standardOutput.write(Data($0))}}
func drain(){DispatchQueue.global(qos:.userInitiated).async{var input=[Float](repeating:0,count:480);var output=[Float]();output.reserveCapacity(480);var count:UInt32=0;var rate:UInt32=48000;var rawPosition:UInt64=0;var timestamp:UInt64=0;var outputPosition:UInt64=0;var outputTimestamp:UInt64=0;var phase:Double=0;var previous:Float=0;var primed=false;var announced=false
 while true{if ma_overflow() != 0{fail("Capture ring overflow or unreliable hardware clock: source lost; explicit retry required")};if !announced && ma_is_ready() != 0{emit([],0,UInt64(ProcessInfo.processInfo.systemUptime*1_000_000_000));announced=true};if ma_pop(&input,&count,&rate,&rawPosition,&timestamp)==0{usleep(2000);continue};guard rate>0 && rate<=384000 && timestamp>0 else{fail("Invalid CoreAudio rate/clock")};let extra=primed ? 1:0;let length=Int(count)+extra;let step=Double(rate)/48000.0
  while phase+1<Double(length){let i=Int(phase);let fraction=Float(phase-Double(i));let a=(extra==1 && i==0) ? previous:input[i-extra];let j=i+1;let b=(extra==1 && j==0) ? previous:input[j-extra];if output.isEmpty{let nativeOffset=phase-Double(extra);outputPosition=UInt64(max(0,(Double(rawPosition)+nativeOffset)*48000.0/Double(rate)));outputTimestamp=UInt64(max(0,Double(timestamp)+nativeOffset*1_000_000_000/Double(rate)))};output.append(a+(b-a)*fraction);phase+=step;if output.count==480{emit(output,outputPosition,outputTimestamp);output.removeAll(keepingCapacity:true)}};phase-=Double(max(0,length-1));previous=input[Int(count)-1];primed=true
 }
}}
final class SystemOutput:NSObject,SCStreamOutput,SCStreamDelegate {
 func stream(_ stream:SCStream,didStopWithError error:Error){fail("ScreenCaptureKit permission revoked or stream stopped: \(error.localizedDescription)")}
 func stream(_ stream:SCStream,didOutputSampleBuffer buffer:CMSampleBuffer,of type:SCStreamOutputType){
  guard type == .audio,buffer.isValid,let format=CMSampleBufferGetFormatDescription(buffer),let description=CMAudioFormatDescriptionGetStreamBasicDescription(format) else{return}
  // Fixed-size stack buffer list avoids callback heap allocation.
  var list=AudioBufferList();var retained:CMBlockBuffer?;var needed=0
  let status=CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(buffer,bufferListSizeNeededOut:&needed,bufferListOut:&list,bufferListSize:MemoryLayout<AudioBufferList>.size,blockBufferAllocator:nil,blockBufferMemoryAllocator:nil,flags:0,blockBufferOut:&retained)
  guard status==noErr,list.mNumberBuffers==1,let pointer=list.mBuffers.mData else{return}
  let stamp=CMTimeConvertScale(CMSampleBufferGetPresentationTimeStamp(buffer),timescale:1_000_000_000,method:.default);guard stamp.isValid && stamp.value>0 else{ma_error();return};let rate=UInt32(description.pointee.mSampleRate);let position=UInt64(Double(stamp.value)*Double(rate)/1_000_000_000);ma_push(pointer.assumingMemoryBound(to:Float.self),UInt32(CMSampleBufferGetNumSamples(buffer)),description.pointee.mChannelsPerFrame,rate,1,position,UInt64(stamp.value))
 }
}
@main struct Main {
 static func main() async {
  guard CommandLine.arguments.count>=2 else{fail("devices | capture <stable-key> <mic|system>")}
  if CommandLine.arguments[1]=="devices" {
   var devices=audioDevices()
   do {let content=try await SCShareableContent.excludingDesktopWindows(true,onScreenWindowsOnly:true);for display in content.displays {devices.append(["key":"screencapturekit:\(display.displayID)","name":"System audio — display \(display.displayID)","kind":"system","is_default":false,"available":true,"experimental":true])}}
   catch {FileHandle.standardError.write(Data("Screen/audio permission unavailable; enable Screen & System Audio Recording in Privacy settings, then restart.\n".utf8))}
   do{FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject:devices))}catch{fail(error.localizedDescription)};return
  }
  guard CommandLine.arguments.count==4 else{fail("Missing explicit stable source selection")};let key=CommandLine.arguments[2];let kind=CommandLine.arguments[3];drain()
  if kind=="system" {
   guard key.hasPrefix("screencapturekit:"),let id=UInt32(key.dropFirst("screencapturekit:".count))else{fail("Invalid ScreenCaptureKit identity")}
   do {let content=try await SCShareableContent.excludingDesktopWindows(true,onScreenWindowsOnly:true);guard let display=content.displays.first(where:{$0.displayID==id})else{fail("Selected system source disappeared; reselect explicitly")}
    let own=content.applications.filter{$0.processID==getpid() || $0.processID==getppid()};let filter=SCContentFilter(display:display,excludingApplications:own,exceptingWindows:[]);let config=SCStreamConfiguration();config.capturesAudio=true;config.excludesCurrentProcessAudio=true;config.sampleRate=48000;config.channelCount=1;config.width=2;config.height=2;config.minimumFrameInterval=CMTime(value:1,timescale:1);let output=SystemOutput();let stream=SCStream(filter:filter,configuration:config,delegate:output);try stream.addStreamOutput(output,type:.audio,sampleHandlerQueue:DispatchQueue(label:"meeting.audio.system",qos:.userInteractive));try await stream.startCapture();ma_ready();while true{try await Task.sleep(nanoseconds:1_000_000_000)}
   }catch{fail("ScreenCaptureKit permission/capture failed: \(error.localizedDescription)")}
  } else {
   guard key.hasPrefix("coreaudio:") else{fail("Invalid CoreAudio identity")};let uid=String(key.dropFirst("coreaudio:".count));let approved=await AVCaptureDevice.requestAccess(for:.audio);guard approved else{fail("Microphone permission denied: enable Microphone in Privacy settings")}
   let engine=AVAudioEngine();let node=engine.inputNode;var address=AudioObjectPropertyAddress(mSelector:kAudioHardwarePropertyDeviceForUID,mScope:kAudioObjectPropertyScopeGlobal,mElement:kAudioObjectPropertyElementMain);var cfuid=uid as CFString;var id=AudioDeviceID(0);var size=UInt32(MemoryLayout<AudioDeviceID>.size);withUnsafePointer(to:&cfuid){ptr in var translation=AudioValueTranslation(mInputData:UnsafeMutableRawPointer(mutating:ptr),mInputDataSize:UInt32(MemoryLayout<CFString>.size),mOutputData:&id,mOutputDataSize:size);size=UInt32(MemoryLayout<AudioValueTranslation>.size);_ = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),&address,0,nil,&size,&translation)}
   guard id != 0,let unit=node.audioUnit,AudioUnitSetProperty(unit,kAudioOutputUnitProperty_CurrentDevice,kAudioUnitScope_Global,0,&id,UInt32(MemoryLayout<AudioDeviceID>.size))==noErr else{fail("Selected CoreAudio microphone disappeared")}
   let format=node.inputFormat(forBus:0);guard format.channelCount>0 else{fail("Microphone has no input channels")}
   var origin:Int64?=nil
   node.installTap(onBus:0,bufferSize:480,format:format){buffer,time in guard let pointers=buffer.floatChannelData,time.isHostTimeValid,time.isSampleTimeValid else{ma_error();return};if origin==nil{origin=time.sampleTime};let(position,overflow)=time.sampleTime.subtractingReportingOverflow(origin!);guard !overflow && position>=0 else{ma_error();return};let channels=UInt32(buffer.format.channelCount);let frames=UInt32(buffer.frameLength);let rate=UInt32(buffer.format.sampleRate);let timestamp=UInt64(AVAudioTime.seconds(forHostTime:time.hostTime)*1_000_000_000);if buffer.format.isInterleaved{ma_push(pointers[0],frames,channels,rate,1,UInt64(position),timestamp)}else{ma_push_planes(UnsafeRawPointer(pointers),frames,channels,rate,UInt64(position),timestamp)}}
   let observer=NotificationCenter.default.addObserver(forName:.AVAudioEngineConfigurationChange,object:engine,queue:nil){_ in fail("CoreAudio endpoint/configuration changed; select the source explicitly again")};_ = observer
   do{try engine.start();ma_ready();while true{try await Task.sleep(nanoseconds:1_000_000_000)}}catch{fail("CoreAudio capture failed: \(error.localizedDescription)")}
  }
 }
}
