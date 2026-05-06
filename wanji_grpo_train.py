from cosmos_rl.dispatcher.data.schema import ChatMessage
from typing import Optional, Any, List, Dict, Union, Callable
from torch.utils.data import Dataset, ConcatDataset, Subset
from datasets import load_dataset
from cosmos_rl.dispatcher.run_web_panel import main as launch_dispatcher
import cosmos_rl.utils.util as util
from cosmos_rl.policy.config import Config

from transformers import AutoTokenizer 
from cosmos_rl.utils.util import basename_from_modelpath
from cosmos_rl.dispatcher.data.packer.base import DataPacker
from cosmos_rl.dispatcher.data.packer import DataPacker, HFVLMDataPacker, Qwen3_VL_DataPacker, Qwen2_5_VLM_DataPacker
from cosmos_rl.utils.logging import logger
import toml
import os
import argparse
import json
import re
import torch
from qwen_vl_utils import process_vision_info

IGNORE_LABEL_ID = -100

FPS = 2.0
MAX_PIXELS = 81920
HIGHT = 448
WIDTH = 448


SYSTEM_MCQ = """
You are an expert computer vision AI assistant analyzing traffic video footage. You have expertise in traffic analysis. Your role is to observe the anomalies traffic events in the video and answer the question with detailed reasoning.

  ## Core Capabilities and Approach

  **Visual Analysis Framework:**
  1. **OBSERVE** - Systematically scan the video for all relevant visual elements
  2. **ASSESS** - Evaluate potential risks and traffic dynamics
  3. **REASON** - Apply traffic rules and safety principles
  4. **ADVISE** - Provide clear, immediate actionable guidance

  **Key Focus Areas:**
  - Traffic signals, signs, and road markings
  - Other vehicles (cars, trucks, motorcycles, bicycles)
  - Pedestrians and vulnerable road users
  - Road conditions and constructions
  - Lane positioning and traffic flow
  - Highway merging and lane changes
  - Parking scenarios and low-speed maneuvers

  **Knowledges**


    **Emergency lanes** typically have the following characteristics that distinguish them from regular traffic lanes:
      - Located on the far left or far right edge of the roadway
      - Often marked with different colored pavement (red, yellow, or distinct asphalt)
      - May have different line markings (solid white lines, hatched markings, or thicker boundary lines)
      - Usually narrower than standard traffic lanes
      - May contain signage indicating "Emergency Lane," "No Stopping Except Emergency," or similar warnings
      - Often have a different surface texture or material
      - Typically free of regular traffic flow
      - May be separated by rumble strips or raised pavement markers
    
    **Illegal Parking** has the following key visual indicators:
      - Vehicles stopped on the main travel lanes
      - Vehicles blocking regular lanes or access points
      - Vehicles stopped in areas with "No Stopping" or "No Parking" signage
      - Vehicles parked on shoulders that are not designated emergency lanes


    **Road construction** indicators include but are not limited to:
      - Orange traffic cones, barrels, or barriers creating work zones
      - Construction vehicles (asphalt pavers, rollers, cement mixers, dump trucks, excavators)
      - Workers in high-visibility clothing (orange, yellow, or lime green vests)
      - Lane closures or narrowed lanes with temporary markings
      - Construction equipment actively operating or parked in work zones
      - Temporary signage indicating construction ahead or lane shifts
      - Fresh asphalt (darker surface), concrete work, or exposed roadbed
      - Reduced speed limit signs specific to work zones
      - Flashing warning lights on construction vehicles or arrow boards

    **Person presence in highway** indicators include but are not limited to:
      - Highways are restricted areas where pedestrian access poses serious safety risks and legal violations
      - Highway intrusions typically involve pedestrians, stranded motorists who have exited vehicles, or individuals crossing restricted areas
      - Common false positives include roadside signs, bridge shadows, overpasses, vegetation, and vehicle-mounted equipment
      - Video quality may vary due to weather conditions, time of day, camera angle, and distance from subjects
      - Detection confidence should account for partial occlusions, motion blur, and perspective distortion

    **Vehicle reversing** has the following key visual indicators:
      - White reverse lights/backup lights illuminated on the vehicle
      - Vehicle moving opposite to the flow of traffic
      - Decreasing distance between the vehicle and fixed reference points behind it
      - Vehicle moving backward relative to lane markings or road features

    **Traffic Congestion** on highways is typically characterized by:
      - Reduced vehicle speeds compared to typical highway flow
      - High vehicle density with minimal spacing between vehicles
      - Stop-and-go traffic patterns
      - Lane occupancy rates exceeding normal capacity
      - Reduced average vehicle velocity across multiple lanes
      - Extended queuing or backup of vehicles

    **Traffic Incidents** on highways include but are not limited to:
      - Vehicle collisions (rear-end, side-impact, multi-vehicle), usually caused by speeding, reckless driving, or driver fatigue.
      - Vehicle breakdowns or stalled vehicles
      - Debris or obstacles on the roadway
      - Overturned or rolled vehicles
      - Fire or smoke from vehicles
      - Animals on the roadwa

    **Motorcycle Presence** has the following key visual indicators:
      - Two-wheeled configuration with visible wheels aligned front-to-back
      - Smaller profile compared to cars and trucks
      - Single rider or passenger positioned in tandem (front-to-back seating)
      - Visible handlebars and exposed engine components
      - May include sport bikes, cruisers, touring motorcycles, or dirt bikes
      - Often positioned between lanes or in standard traffic lanes
      - Riders typically wear helmets and protective gear

  
    **Thrown objects** on highways can include but are not limited to:
      - Objects may be stationary on the road surface or moving through the air, like plastic bags, bottles, papers, clothing items, small debris, etc.
      - Objects may blend with road surface colors or be highly contrasting

    
    **lane changes across solid lane lines** can include but are not limited to:
      - Solid white or yellow lines indicate areas where lane changes are prohibited for safety reasons
      - Dashed/broken lane lines indicate areas where lane changes are permitted when safe
      - A lane change violation occurs when a vehicle's path crosses over a solid line while transitioning between lanes
      - Vehicle positioning should be tracked relative to lane markings throughout the duration of any lane change maneuver
      - Consider the vehicle's trajectory and timing - brief tire contact during normal driving within a lane is different from an active lane change across the line


  Analyze the provided video footage frame-by-frame to detect and identify instances. When analyzing the video, the assistant should:
    - Scan each frame
    - Identify the emergency lane location in the video frame by detecting lane markings, road edges, and spatial positioning relative to regular traffic lanes
    - Track all vehicles that enter, stop, occupy, or travel within the emergency lane throughout the video duration
    - Track all vehicles motion by frame-by-frame analysis to confirm backward movement, multiple visual cues to validate reversing behavior
    - Identify the construction vehicles and construction workers
    - Detect any human figures present in the highway lanes, shoulders, median strips, or other restricted highway areas and track the movement and location of detected persons throughout the video timeline
    - Distinguish between congested and non-congested segments based on vehicle density, speed, and flow patterns
    - Distinguish between normal traffic variations (congestion, lane changes, speed differences) and actual incidents requiring attention.
    - Identify the presence of motorcycles and track motorcycle presence throughout the video duration
    - Identify all visible lane markings in the video and classify them as solid or dashed lines, and determine if lane changes occur across solid lines versus dashed lines
    - Detect the thrown objects on the highway and track the movement and location of thrown objects throughout the video duration
  


Structure your response in this format:
<think>
  [reasoning process for each event]
  - emergency lane: [leftmost lane, rightmost lane, no emergency lane]
  - illegal parking: [yes, no]
  - traffic incidents: [yes, no]
  - construction: [yes, no]
  - person presence in highway: [yes, no]
  - vehicle reversing: [yes, no]
  - traffic congestion: [light, moderate, heavy]
  - motorcycle presence: [yes, no]
  - thrown objects: [yes, no]
  - lane changes across solid lane lines: [yes, no, unknown]
</think> 
<answer> 
  your answer
</answer>

"""

WANJI_MCQ_ALL = """
there are 11 possible events in the video:
A. Illegal Parking
B. Emergency Lane Occupancy
C. Traffic Accident
D. Person Presence in highway
E. Motorcycle Presence
F. Heavy Congestion
G. Road Construction
H. Vehicle Reversing
I. Normal
J. Thrown Objects
K. Lane Change over Solid Line

you first analyze the video and evaluate each provided option separately based on the knowledges you have to determine its presence. 
For each option that is identified in the video, include it in the final answer. 
There could be mulitple events present in the video, you should give all possible events options you found in your answer.
If None of the events are present in the video. you only need to give "I" in your answer.
you should provide ONLY the letter(s) of the option(s) found in the video.
"""

class WanjiGRPODataset(Dataset):
    def __init__(self, dataset_path: str, oversample_ratio: float = 1.0, video_frame_min_pixels: int = 131072, video_frame_max_pixels: int = 786432, video_fps: float = 2.0):
        self.dataset = self.load_wanji_dataset(dataset_path, oversample_ratio)
        self.mm_files_paths = os.path.dirname(dataset_path)
        self.oversample_ratio = oversample_ratio
        self.video_frame_min_pixels = video_frame_min_pixels
        self.video_frame_max_pixels = video_frame_max_pixels
        self.video_fps = video_fps

    def load_wanji_dataset(self, label_path: str, oversample_ratio: float = 1.0):
        with open(label_path, 'r') as f:
            data = json.load(f)
        if oversample_ratio > 1.0:
            data = data * int(oversample_ratio)
        return data

    def setup(self, config: Config, tokenizer: AutoTokenizer,  *args, **kwargs):
        self.config = config
        self.tokenizer = tokenizer
        

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, idx: int) -> tuple[str, str]:
        '''
            Return a tuple of (prompt, reference answer)
        '''
        payload = self.dataset[idx]
        user_prompt = WANJI_MCQ_ALL
        sys_conv = [
            {
                "type": "text",
                "text": SYSTEM_MCQ,
            },
        ]
        user_conv = [
            {
                "type": "video",
                "video": os.path.join(self.mm_files_paths, payload["video"]),
                "max_pixels": self.video_frame_max_pixels,
                "min_pixels": self.video_frame_min_pixels,
                # "resized_height": HIGHT,
                # "resized_width": WIDTH,
                "fps": self.video_fps,
            },
            {
                "type": "text",
                "text": user_prompt,
            },
        ]
        # multi_modal_content = 
        # user_conv.insert(0, multi_modal_content)
        conversations = [
            {
                "role": "system",
                "content": sys_conv,
            },
            {
                "role": "user",
                "content": user_conv,
            },
        ]
        return conversations

    def get_reference_answer(self, idx: int) -> str:
        payload = self.dataset[idx]
        return payload["answer"]

class WanjiGRPOValDataset(WanjiGRPODataset):
    '''
    This is a validation dataset for Cosmos GRPO, which is used to evaluate the performance of the model.
    It should be used in the launcher to evaluate the model during training.
    '''
    def setup(self, config: Config, tokenizer: AutoTokenizer, *args, **kwargs):
        if not config.validation.enable:
            logger.warning("Validation is not enabled in the config. Skipping setup for WanjiGRPOValDataset.")
            return

        self.config = config
        self.tokenizer = tokenizer
        


def multi_choice_reward_fn(
    to_be_evaluated: str, reference: Union[str, None], **kwargs
) -> float:
    """Reward function for multiple choice questions using Scheme A (Balanced Approach).
    
    Handles answers that may contain multiple options (e.g., 'A', 'B', 'C' or 'ABC').
    Student answers must have <answer> tags, but reference can be direct option strings.
    
    Reward Rules (Scheme A - Balanced Approach):
        1) Perfect match (student == GT): 1.0
           Example: student="ABC", GT="ABC" -> 1.0
           
        2) Partial correct (no wrong options, but missing some):
           reward = 0.3 + 0.7 * (num_correct / num_gt)
           Examples:
           - student="AB", GT="ABC" -> 0.3 + 0.7 * (2/3) = 0.767
           - student="A", GT="ABC" -> 0.3 + 0.7 * (1/3) = 0.533
           
        3) Has incorrect options (balanced scoring):
           reward = (num_correct - 0.5 * num_incorrect) / num_gt
           Lower bound: -0.5
           Examples:
           - student="ABD", GT="ABC" -> (2 - 0.5*1)/3 = 0.500 (2 correct, 1 wrong)
           - student="AD", GT="ABC" -> (1 - 0.5*1)/3 = 0.167 (1 correct, 1 wrong)
           - student="D", GT="ABC" -> (0 - 0.5*1)/3 = -0.167 (pure error)
           - student="DEFG", GT="ABC" -> max((0 - 0.5*4)/3, -0.5) = -0.5 (lower bound)
           
        4) Student answer must have <answer> tags, otherwise return 0.0

    Args:
        to_be_evaluated: Student's answer (must contain <answer> tags)
        reference: Ground truth answer (can be with or without tags)
        **kwargs: Additional arguments (not used)

    Returns:
        float: Reward score in range [-0.5, 1.0]
              - 1.0: Perfect match
              - 0.3~1.0: Only missing options
              - -0.5~1.0: Has incorrect options (balanced scoring)
              - 0.0: No answer tags or invalid input
    """

    reward = 0.0
    try:
        # Extract answer from solution if it has think/answer tags
        sol_match = re.search(r"<answer>(.*?)</answer>", reference, re.DOTALL)
        ground_truth = sol_match.group(1).strip() if sol_match else reference.strip()

        # Extract answer from content if it has think/answer tags
        content_match = re.search(r"<answer>(.*?)</answer>", to_be_evaluated, re.DOTALL)
        student_answer = (
            content_match.group(1).strip() if content_match else to_be_evaluated.strip()
        )
        
        
        
        # Student answer must have tags, but reference can be direct options
        if not content_match:
            return 0.0
        
        # Normalize answers by removing spaces and converting to uppercase
        student_answer_norm = re.sub(r'\s+', '', student_answer.upper())
        ground_truth_norm = re.sub(r'\s+', '', ground_truth.upper())
        
        # Extract individual option letters from both answers
        student_options = set(re.findall(r'[A-Z]', student_answer_norm))
        ground_truth_options = set(re.findall(r'[A-Z]', ground_truth_norm))

        # Calculate reward based on option matching
        if len(ground_truth_options) == 0:
            # If no valid options found in ground truth, fall back to exact string matching
            if student_answer_norm == ground_truth_norm:
                reward = 1.0
        else:
            # New reward logic:
            # 1. All correct: 1.0
            # 2. Partial correct (no wrong options): proportional score based on correct ratio
            # 3. Has wrong options: negative penalty
            
            if student_options == ground_truth_options:
                # Perfect match - all options correct
                reward = 1.0
            elif len(student_options) > 0 and len(ground_truth_options) > 0:
                # Calculate correct, incorrect, and missing options
                correct_options = student_options & ground_truth_options  # Intersection
                incorrect_options = student_options - ground_truth_options  # Student has but GT doesn't
                missing_options = ground_truth_options - student_options  # GT has but student doesn't
                
                num_correct = len(correct_options)
                num_incorrect = len(incorrect_options)
                num_missing = len(missing_options)
                num_gt = len(ground_truth_options)
                
                # Reward calculation 
                # Has incorrect options - balanced scoring
                # Each correct option contributes: +1/num_gt
                # Each incorrect option contributes: -0.5/num_gt (half penalty)
                # This rewards partial correctness while penalizing errors
                reward = (num_correct - 0.5 * num_incorrect) / num_gt
                # Set lower bound to avoid excessive penalty
                reward = max(reward, -1.0)
                    
    except Exception:
        reward = 0.0
    
    logger.info(f'binz --- to_be_evaluated: {to_be_evaluated}')
    logger.info(f'binz --- reference: {reference}')

    logger.info(f' binz --- student_options: {student_options}, ground_truth_options: {ground_truth_options} MCQ reward: {reward}')
    if 'prompt' in kwargs:
        prompt = kwargs['prompt']
        for item in prompt:
            if item.role == 'user':
                for content in item.content:
                    if content['type'] == 'video':
                        video_path = content['video']
                        break
        logger.info(f'binz --- video_path: {video_path}')
    return reward


def format_reward_fn(
    to_be_evaluated: str, reference: Union[str, None], **kwargs
) -> float:
    try:
        pattern = r"^<think>(.*?)</think>\s*<answer>(.*?)</answer>$"
        # pattern = r"^<think>([^<]*(?:<(?!/?think>)[^<]*)*)<\/think>\n\n<answer>([\s\S]*?)<\/answer>$"
        match = re.search(pattern, to_be_evaluated, re.DOTALL)
        # if the format is not correct, reward is 0
        
        if match is None or len(match.groups()) != 2:
            logger.info(f'binz --- format_reward_fn doesnot match: {to_be_evaluated}')
            return 0.0
        else:
            return 1.0
    except Exception as e:  # noqa: BLE001
        # logger.debug("Exception in format_reward_func: %s", e)
        print(f'binz Exception in format_reward_func: {e}')
        return 0.0


def custom_reward_fn(
    to_be_evaluated: str, reference: Optional[str] = None, *args, **kwargs
) -> float:
    
    multi_choice_reward = multi_choice_reward_fn(to_be_evaluated, reference, *args, **kwargs)
    format_reward = format_reward_fn(to_be_evaluated, reference, *args, **kwargs)
    logger.info(f'binz --- multi_choice_reward_fn: {multi_choice_reward}, format_reward: {format_reward}')

    return sum(
        [
            1.5*multi_choice_reward,
            0.5*format_reward,
        ]
    )

class DemoDataPacker(DataPacker):
    """
    This is a demo data packer that wraps the underlying data packer of the selected model.
    This is meaningless for this example, but useful for explaining:
        - how dataset data is processed and collated into a mini-batch for rollout engine;
        - how rollout output is processed and collated into a mini-batch for policy model;
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Check source code of Qwen2_5_VLM_DataPacker to see how it's implemented
        self.underlying_data_packer = HFVLMDataPacker()

    def setup(self, config: Config, *args, **kwargs):
        """
        This method is optional and get called by launcher after being mounted
        `config`: config;
        """
        super().setup(config, *args, **kwargs)
        self.underlying_data_packer.setup(config, *args, **kwargs)

    def get_rollout_input(self, item: Any) -> Any:
        """
        Convert dataset item into what rollout engine (e.g. vllm) expects
        """
        return self.underlying_data_packer.get_rollout_input(item)

    def rollout_collate_fn(self, items: List[Any]) -> Any:
        """
        Collate the rollout inputs into a mini-batch for rollout engine
        """
        return self.underlying_data_packer.rollout_collate_fn(items)

    def get_policy_input(
        self,
        item: Any,
        rollout_output: Union[str, List[int]],
        n_ignore_prefix_tokens: int = 0,
    ) -> Any:
        """
        Process samples & rollout output before collating them into a mini-batch
        """
        return self.underlying_data_packer.get_policy_input(
            item, rollout_output, n_ignore_prefix_tokens
        )

    def policy_compute_max_len(self, processed_samples: List[Any]) -> int:
        """
        Compute the maximum sequence length of the mini-batch
        """
        return self.underlying_data_packer.policy_compute_max_len(processed_samples)

    def policy_collate_fn(
        self, processed_samples: List[Any], computed_max_len: int
    ) -> Dict[str, Any]:
        """
        Collate the mini-batch into the kwargs required by the policy model
        """
        return self.underlying_data_packer.policy_collate_fn(
            processed_samples, computed_max_len
        )



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, required=True)
    args = parser.parse_known_args()[0]
    with open(args.config, "r") as f:
        config = toml.load(f)
    config = Config.from_dict(config)
    # logger.info(f'binz config: {config}')
    # logger.info(f'binz config.custom: {config.custom}')
    oversample_ratio = config.custom.get("oversample_ratio", 1.0)
    video_frame_min_pixels = config.custom.get("video_frame_min_pixels", 131072)
    video_frame_max_pixels = config.custom.get("video_frame_max_pixels", 786432)
    video_fps = config.custom.get("video_fps", 2.0)

    dataset = WanjiGRPODataset(config.custom['trainset_path']   , oversample_ratio=oversample_ratio, video_frame_min_pixels=video_frame_min_pixels, video_frame_max_pixels=video_frame_max_pixels, video_fps=video_fps)
    logger.info(f'binz train dataset length: {len(dataset)}')
    val_dataset = WanjiGRPOValDataset(config.custom['evalset_path']) if config.validation.enable else None
    logger.info(f'binz val dataset length: {len(val_dataset)}')
    launch_dispatcher(
        dataset=dataset,
        reward_fns=[custom_reward_fn],
        data_packer=DemoDataPacker(),
        val_dataset=val_dataset,
        val_reward_fns=[multi_choice_reward_fn],
        val_data_packer=DemoDataPacker(),
    )
