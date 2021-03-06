import sys
import sensenet

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.autograd as autograd
from torch.autograd import Variable
from tensorboardX import SummaryWriter
import numpy as np
from torch.distributions import Categorical
from itertools import count
from collections import namedtuple
import time,os,math,inspect,re,random,argparse


parser = argparse.ArgumentParser(description='SenseNet reinforce example')
parser.add_argument('--gamma', type=float, default=0.99, metavar='G', help='discount factor (default: 0.99)')
parser.add_argument('--epsilon', type=float, default=0.6, metavar='G', help='epsilon value for random action (default: 0.6)')
parser.add_argument('--seed', type=int, default=42, metavar='N', help='random seed (default: 42)')
parser.add_argument('--lr', type=float, default=0.001, metavar='G', help='learning rate')
parser.add_argument('--log-interval', type=int, default=10, metavar='N', help='interval between training status logs (default: 10)')
parser.add_argument('--render', action='store_true', help='render the environment')
parser.add_argument('--debug', action='store_true', help='debug')
parser.add_argument('--gpu', action='store_true', help='use GPU')
parser.add_argument('--log', type=str, help='log experiment to tensorboard')
parser.add_argument('--model_path', type=str, help='path to store/retrieve model at')
parser.add_argument('--name', type=str, help='name for logs/model')
parser.add_argument('--mode', type=str, default="train", help='train/test/all model')
parser.add_argument('--environment','-e', type=str, default="TouchWandEnv-v0")
parser.add_argument('--num_episodes', type=int, default=1000, help='number of episodes')
parser.add_argument('--max_steps', type=int, default=500, help='number of steps per episode')
#TODO fix obj_type working with data_path together
#parser.add_argument('--data_path', default=None, help='path to training data')
#parser.add_argument('--obj_type', type=str, default="obj", help='obj or stl')
args = parser.parse_args()

if args.name:
  model_path = "./models/"+args.name
  log_name = args.name
else:
  if args.log:
    log_name = args.log
  else:
    log_name = None
  if args.model_path:
    model_path = args.model_path
  else:
    model_path = None
# Command to visualize logs:
#   tensorboard --logdir runs
writer = SummaryWriter()
SavedAction = namedtuple('SavedAction', ['action', 'value'])


class Policy(nn.Module):

  def __init__(self,observation_space_n,action_space_n):
    super(Policy, self).__init__()
    self.affine1 = nn.Linear(observation_space_n, 256)
    self.action1 = nn.Linear(256, 128)
    self.value1 = nn.Linear(256, 128)
    self.action_head = nn.Linear(128, action_space_n)
    self.value_head = nn.Linear(128, 1)
    self.saved_actions = []
    self.rewards = []
    self.saved_log_probs = []
    self.init_weights()

  def init_weights(self):
    self.affine1.weight.data.uniform_(-0.1, 0.1)
    self.action1.weight.data.uniform_(-0.1, 0.1)
    self.action_head.weight.data.uniform_(-0.1, 0.1)
    self.value1.weight.data.uniform_(-0.1, 0.1)

  def forward(self, x):
    if args.gpu and torch.cuda.is_available():
      # TODO: clean way to access "args.gpu" from here.
      x = x.cuda()
    x = F.relu(self.affine1(x))
    xa = F.relu(self.action1(x))
    xv = F.relu(self.value1(x))
    action_scores = self.action_head(xa)
    state_values = self.value_head(xv)
    return F.softmax(action_scores)
    #return F.softmax(action_scores), state_values

def select_training_action(state,n_actions,epsilon):
  if np.random.rand() < 0.25:
    return 0 #forward
    #print("random")
    #return np.random.choice(n_actions)

  else:
    return select_action(state,n_actions,epsilon)

def select_action(state,n_actions,epsilon=0.2):
  if np.random.rand() < epsilon:
    return np.random.choice(n_actions)
  else:
    state = torch.from_numpy(state).float().unsqueeze(0)
    probs = policy(Variable(state))
    m = Categorical(probs)
    action = m.sample()
    policy.saved_log_probs.append(m.log_prob(action))
    return action.data[0]

def finish_episode_learning(model, optimizer):
  R = 0
  policy_loss = []
  rewards = []
  for r in policy.rewards[::-1]:
    R = r + args.gamma * R
    rewards.insert(0, R)
  rewards = torch.Tensor(rewards)
  rewards = (rewards - rewards.mean()) / (rewards.std() + np.finfo(np.float32).eps)
  for log_prob, reward in zip(policy.saved_log_probs, rewards):
    policy_loss.append(-log_prob * reward)
  optimizer.zero_grad()
  policy_loss = torch.cat(policy_loss).sum()
  policy_loss.backward()
  optimizer.step()
  del model.rewards[:]
  del model.saved_log_probs[:]


# Training:

print(vars(args))
env = sensenet.make(args.environment,vars(args))
#env = sensenet.make(args.environment,{'render':args.render,'debug':args.debug})

print("action space: ",env.action_space,env.action_space.n)
print("class count: ",env.classification_n())
policy = Policy(env.observation_space.shape[0] * env.observation_space.shape[1],env.action_space.n)
if args.gpu and torch.cuda.is_available():
  policy.cuda()
if model_path:
  if os.path.exists(model_path+"/model.pkl"):
    print("loading pretrained models")
    policy.load_state_dict(torch.load(model_path+"/model.pkl"))

criterion = nn.MSELoss()
optimizer = torch.optim.Adam(policy.parameters(), lr=args.lr)


running_reward = 10
total_steps = 0
touched_episodes = 0
steps_to_first_touch = []

if args.mode == "train":

  for i_episode in range(args.num_episodes):
    # New object (aka new episode)
    observation = env.reset()
    average_activated_pixels = []
    touch_count = 0
    observed_touches = []
    print("episode:", i_episode)

    for step in range(args.max_steps):
      # Move and touch the object in this loop. Record touches only as inputs.
      action = select_training_action(observation,env.action_space.n,args.epsilon)
      observation, reward, done, info = env.step(action)
      policy.rewards.append(reward)
      average_activated_pixels.append(np.mean(observation))
      if env.is_touching():
        if args.debug:
          print("touching")
        observed_touches.append(observation.reshape(env.observation_space.shape[0],env.observation_space.shape[1]))
        touch_count += 1
        if touch_count == 1:
          steps_to_first_touch.append(step)
        if args.debug:
          print("touch at step ", step, " in episode ", i_episode)
      #if done:
      #  break

    if len(observed_touches) != 0:
      touched_episodes += 1

    running_reward = running_reward * 0.99 + step * 0.01
    total_steps +=1
    print("  learning...")
    print(touch_count, "touches in episode", i_episode)
    finish_episode_learning(policy, optimizer)

    if log_name:
      writer.add_scalar(log_name+"/reward",running_reward,total_steps)
      writer.add_scalar(log_name+"/touches",len(observed_touches),total_steps)
      #writer.add_scalar(log_name+"/average_activated_pixels",np.mean(average_activated_pixels),total_steps)
    if i_episode % args.log_interval == 0:
      print('  Episode {}\tLast length: {:5d}\tAverage length: {:.2f}'.format(i_episode, step, running_reward))
    if running_reward > 5000: #env.spec.reward_threshold:
      print("  Solved! Running reward is now {} and the last episode runs to {} time steps!".format(running_reward, t))
      break
    if model_path:
      env.mkdir_p(model_path)
      torch.save(policy.state_dict(), os.path.join(model_path, 'policy.pkl' ))
  print("touched", touched_episodes, "times in", args.num_episodes,"episodes", (touched_episodes/args.num_episodes))
  if len(steps_to_first_touch) > 0:
    print("average steps to first touch", np.mean(steps_to_first_touch))
